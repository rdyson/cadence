#!/usr/bin/env python3
"""
cadence deploy script
Builds cadence.json, syncs frontend to S3, updates Lambda, invalidates CloudFront.
Usage: python scripts/deploy.py [--skip-build] [--skip-lambda]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message=".*Boto3 will no longer support.*")
import boto3
import yaml


def load_config(config_path: str = "cadence.yaml") -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"  Error: command failed with exit code {result.returncode}")
        sys.exit(1)
    return result


def deploy(config_path: str = "cadence.yaml", skip_build: bool = False, skip_lambda: bool = False) -> None:
    config = load_config(config_path)
    aws = config.get("aws", {})
    region = aws.get("region", "eu-west-2")
    bucket = aws.get("s3_bucket", "")
    lambda_name = "cadence-api"
    table_name = aws.get("dynamodb_table", "cadence-study")
    cf_url = aws.get("cloudfront_url", "")

    if not bucket:
        print("Error: aws.s3_bucket not set in cadence.yaml — run scripts/setup-aws.sh first.")
        sys.exit(1)

    print("\n🚀 Deploying Cadence...")
    print(f"   Region: {region}")
    print(f"   Bucket: {bucket}\n")

    # Step 1: Build cadence.json
    if not skip_build:
        print("▶ Building cadence.json...")
        run([sys.executable, "scripts/build.py", "--config", config_path])
        print()

    # Step 2: Sync frontend to S3
    print("▶ Syncing frontend to S3...")
    frontend_dir = Path("frontend")
    s3 = boto3.client("s3", region_name=region)
    content_types = {
        ".html": "text/html",
        ".js": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".ico": "image/x-icon",
        ".png": "image/png",
        ".svg": "image/svg+xml",
    }

    uploaded = 0
    for file in frontend_dir.rglob("*"):
        if file.is_file():
            key = str(file.relative_to(frontend_dir))
            ct = content_types.get(file.suffix, "application/octet-stream")
            s3.upload_file(
                str(file),
                bucket,
                key,
                ExtraArgs={"ContentType": ct},
            )
            print(f"  ✓ {key}")
            uploaded += 1

    print(f"  Uploaded {uploaded} files\n")

    # Step 3: Update Lambda
    if not skip_lambda:
        print("▶ Updating Lambda function...")
        import zipfile, io
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write("backend/lambda_function.py", "lambda_function.py")
        buffer.seek(0)

        lam = boto3.client("lambda", region_name=region)
        lam.update_function_code(
            FunctionName=lambda_name,
            ZipFile=buffer.read(),
        )
        # Wait for code update to finish before updating configuration
        waiter = lam.get_waiter("function_updated_v2")
        waiter.wait(FunctionName=lambda_name)
        # Update env vars in case table name changed
        lam.update_function_configuration(
            FunctionName=lambda_name,
            Environment={"Variables": {"DYNAMODB_TABLE": table_name}},
        )
        print("  ✓ Lambda updated\n")

    # Step 4: Invalidate CloudFront (if configured)
    if cf_url:
        print("▶ Invalidating CloudFront cache...")
        distribution_id = cf_url.split(".")[0].replace("https://", "")
        try:
            cf = boto3.client("cloudfront", region_name="us-east-1")
            # Get distribution ID from domain
            dists = cf.list_distributions()
            dist_id = None
            for d in dists.get("DistributionList", {}).get("Items", []):
                if d.get("DomainName", "").startswith(distribution_id):
                    dist_id = d["Id"]
                    break
            if dist_id:
                cf.create_invalidation(
                    DistributionId=dist_id,
                    InvalidationBatch={
                        "Paths": {"Quantity": 1, "Items": ["/*"]},
                        "CallerReference": str(hash(str(Path.cwd()))),
                    },
                )
                print("  ✓ CloudFront invalidation created\n")
            else:
                print("  ⚠ CloudFront distribution not found — skipping invalidation\n")
        except Exception as e:
            print(f"  ⚠ CloudFront invalidation failed: {e}\n")

    print("================================")
    print("  ✅ Deploy complete!")
    if cf_url:
        print(f"  Dashboard: {cf_url}")
    else:
        print(f"  Dashboard: http://{bucket}.s3-website.{region}.amazonaws.com")
        print("  (Set aws.cloudfront_url in cadence.yaml to use CloudFront)")
    print("================================\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy Cadence to AWS")
    parser.add_argument("--config", "-c", default="cadence.yaml")
    parser.add_argument("--skip-build", action="store_true", help="Skip cadence.json build step")
    parser.add_argument("--skip-lambda", action="store_true", help="Skip Lambda update")
    args = parser.parse_args()
    deploy(args.config, args.skip_build, args.skip_lambda)
