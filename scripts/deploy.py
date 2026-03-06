#!/usr/bin/env python3
"""
cadence deploy script
Builds cadence.json, syncs frontend to S3, updates Lambda, invalidates CloudFront.
Uses AWS CLI under the hood so credentials from `aws login` work automatically.
Usage: python scripts/deploy.py [--skip-build] [--skip-lambda]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml


def load_config(config_path: str = "cadence.yaml") -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def run(cmd: list[str], capture: bool = False, **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=capture, text=capture, **kwargs)
    if result.returncode != 0:
        if capture and result.stderr:
            print(f"  Error: {result.stderr.strip()}")
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
    content_types = {
        ".html": "text/html",
        ".js": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".ico": "image/x-icon",
        ".png": "image/png",
        ".svg": "image/svg+xml",
    }

    frontend_dir = Path("frontend")
    uploaded = 0
    for file in frontend_dir.rglob("*"):
        if file.is_file():
            key = str(file.relative_to(frontend_dir))
            ct = content_types.get(file.suffix, "application/octet-stream")
            run([
                "aws", "s3", "cp", str(file), f"s3://{bucket}/{key}",
                "--content-type", ct,
                "--region", region,
            ], capture=True)
            print(f"  ✓ {key}")
            uploaded += 1

    print(f"  Uploaded {uploaded} files\n")

    # Step 3: Update Lambda
    if not skip_lambda:
        print("▶ Updating Lambda function...")
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write("backend/lambda_function.py", "lambda_function.py")

        run([
            "aws", "lambda", "update-function-code",
            "--function-name", lambda_name,
            "--zip-file", f"fileb://{tmp_path}",
            "--region", region,
        ], capture=True)

        # Wait for code update to finish
        run([
            "aws", "lambda", "wait", "function-updated-v2",
            "--function-name", lambda_name,
            "--region", region,
        ], capture=True)

        # Update env vars in case table name changed
        env_json = json.dumps({"Variables": {"DYNAMODB_TABLE": table_name}})
        run([
            "aws", "lambda", "update-function-configuration",
            "--function-name", lambda_name,
            "--environment", env_json,
            "--region", region,
        ], capture=True)

        Path(tmp_path).unlink(missing_ok=True)
        print("  ✓ Lambda updated\n")

        # Update auth triggers Lambda if it exists
        auth_triggers = Path("backend/auth_triggers.py")
        if auth_triggers.exists():
            trigger_name = "cadence-auth-triggers"
            # Check if the Lambda exists before trying to update
            check = subprocess.run(
                ["aws", "lambda", "get-function", "--function-name", trigger_name, "--region", region],
                capture_output=True, text=True,
            )
            if check.returncode == 0:
                print("▶ Updating auth triggers Lambda...")
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp2:
                    tmp2_path = tmp2.name
                    with zipfile.ZipFile(tmp2, "w", zipfile.ZIP_DEFLATED) as zf:
                        zf.write("backend/auth_triggers.py", "auth_triggers.py")
                ses_sender = config.get("ses_sender_email", "")
                run([
                    "aws", "lambda", "update-function-code",
                    "--function-name", trigger_name,
                    "--zip-file", f"fileb://{tmp2_path}",
                    "--region", region,
                ], capture=True)
                run([
                    "aws", "lambda", "wait", "function-updated-v2",
                    "--function-name", trigger_name,
                    "--region", region,
                ], capture=True)
                if ses_sender:
                    env_json2 = json.dumps({"Variables": {"SES_SENDER": ses_sender}})
                    run([
                        "aws", "lambda", "update-function-configuration",
                        "--function-name", trigger_name,
                        "--environment", env_json2,
                        "--region", region,
                    ], capture=True)
                Path(tmp2_path).unlink(missing_ok=True)
                print("  ✓ Auth triggers Lambda updated\n")

    # Step 4: Invalidate CloudFront (if configured)
    if cf_url:
        print("▶ Invalidating CloudFront cache...")
        cf_hostname = cf_url.replace("https://", "").replace("http://", "").strip("/")
        try:
            # Find distribution ID by alias
            result = run([
                "aws", "cloudfront", "list-distributions",
                "--query", f"DistributionList.Items[?Aliases.Items[?contains(@, '{cf_hostname}')]].Id | [0]",
                "--output", "text",
                "--region", "us-east-1",
            ], capture=True)
            dist_id = result.stdout.strip()

            if dist_id and dist_id != "None":
                caller_ref = str(hash(str(Path.cwd())))
                run([
                    "aws", "cloudfront", "create-invalidation",
                    "--distribution-id", dist_id,
                    "--paths", "/*",
                    "--region", "us-east-1",
                ], capture=True)
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
