#!/usr/bin/env python3
"""
cadence — tear down all AWS infrastructure
Deletes the CloudFormation stack and the Lambda deployment bucket.
Replaces teardown-aws.sh.

Usage: python3 scripts/teardown.py [--config cadence.yaml]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
STACK_NAME = "cadence"


def load_config(config_path: Path) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def run(cmd: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=capture, text=capture)
    if result.returncode != 0:
        if capture and result.stderr:
            print(f"  Error: {result.stderr.strip()}")
        sys.exit(1)
    return result


def run_quiet(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def get_account_id() -> str:
    result = run(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"], capture=True)
    return result.stdout.strip()


def empty_s3_bucket(bucket: str, region: str) -> None:
    """Empty an S3 bucket so it can be deleted by CloudFormation."""
    check = run_quiet(["aws", "s3api", "head-bucket", "--bucket", bucket, "--region", region])
    if check.returncode == 0:
        print(f"  Emptying bucket: {bucket}")
        run_quiet(["aws", "s3", "rm", f"s3://{bucket}", "--recursive", "--region", region])


def delete_lambda_bucket(region: str, account_id: str) -> None:
    """Delete the Lambda deployment bucket (not managed by CloudFormation)."""
    bucket = f"cadence-lambda-{account_id}-{region}"
    check = run_quiet(["aws", "s3api", "head-bucket", "--bucket", bucket, "--region", region])
    if check.returncode == 0:
        print(f"  Emptying Lambda bucket: {bucket}")
        run_quiet(["aws", "s3", "rm", f"s3://{bucket}", "--recursive", "--region", region])
        run_quiet(["aws", "s3api", "delete-bucket", "--bucket", bucket, "--region", region])
        print(f"  Deleted Lambda bucket: {bucket}")
    else:
        print(f"  Lambda bucket not found: {bucket}")


def disable_cloudfront(region: str) -> None:
    """Disable any CloudFront distributions in the stack before deletion.
    CloudFormation requires distributions to be disabled before deleting."""
    result = run_quiet([
        "aws", "cloudformation", "describe-stack-resources",
        "--stack-name", STACK_NAME, "--region", region,
        "--query", "StackResources[?ResourceType=='AWS::CloudFront::Distribution'].PhysicalResourceId",
        "--output", "json",
    ])
    if result.returncode != 0:
        return

    dist_ids = json.loads(result.stdout or "[]")
    for dist_id in dist_ids:
        if not dist_id:
            continue
        # Check if enabled
        status_result = run_quiet([
            "aws", "cloudfront", "get-distribution",
            "--id", dist_id,
            "--query", "Distribution.DistributionConfig.Enabled",
            "--output", "text",
        ])
        if status_result.stdout.strip().lower() == "true":
            print(f"  Disabling CloudFront distribution: {dist_id}")
            etag_result = run_quiet([
                "aws", "cloudfront", "get-distribution-config",
                "--id", dist_id, "--query", "ETag", "--output", "text",
            ])
            etag = etag_result.stdout.strip()

            config_result = run_quiet([
                "aws", "cloudfront", "get-distribution-config",
                "--id", dist_id, "--query", "DistributionConfig", "--output", "json",
            ])
            import json as json_mod
            dist_config = json_mod.loads(config_result.stdout)
            dist_config["Enabled"] = False

            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
                json_mod.dump(dist_config, tmp)
                tmp_path = tmp.name

            run_quiet([
                "aws", "cloudfront", "update-distribution",
                "--id", dist_id, "--if-match", etag,
                "--distribution-config", f"file://{tmp_path}",
            ])
            Path(tmp_path).unlink()

            print(f"  Waiting for distribution to disable (~5 minutes)...")
            run_quiet([
                "aws", "cloudfront", "wait", "distribution-deployed", "--id", dist_id,
            ])
            print(f"  Distribution disabled")


def clean_config(config_path: Path) -> None:
    """Remove generated resource IDs from cadence.yaml."""
    with open(config_path) as f:
        config = yaml.safe_load(f)
    aws = config.get("aws", {})
    for key in ["cognito_user_pool_id", "cognito_client_id", "api_url",
                "s3_bucket", "cloudfront_url", "cloudfront_distribution_id"]:
        aws.pop(key, None)
    config["aws"] = aws
    with open(config_path, "w") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    print("  Removed generated values from cadence.yaml")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Tear down Cadence AWS infrastructure")
    parser.add_argument("--config", "-c", default="cadence.yaml")
    args = parser.parse_args()

    os.chdir(ROOT)
    config_path = Path(args.config)
    config = load_config(config_path)
    aws_cfg = config.get("aws", {})
    region = aws_cfg.get("region", "eu-west-2")
    account_id = get_account_id()

    print("=================================")
    print("  Cadence Teardown")
    print(f"  Region: {region}")
    print(f"  Stack:  {STACK_NAME}")
    print("=================================\n")
    print("This will permanently delete ALL Cadence AWS resources.")
    print("DynamoDB data, Cognito users, and all infrastructure will be lost.\n")

    confirm = input("Type 'destroy' to confirm: ")
    if confirm != "destroy":
        print("Aborted.")
        sys.exit(0)
    print()

    # Check stack exists
    check = run_quiet([
        "aws", "cloudformation", "describe-stacks",
        "--stack-name", STACK_NAME, "--region", region,
    ])
    if check.returncode != 0:
        print("  Stack not found. Nothing to tear down.")
    else:
        # Empty S3 buckets (CFn can't delete non-empty buckets)
        s3_bucket = aws_cfg.get("s3_bucket", "")
        if s3_bucket:
            print("Step 1: Emptying S3 buckets")
            empty_s3_bucket(s3_bucket, region)

        # Disable CloudFront (must be disabled before CFn can delete)
        print("\nStep 2: Disabling CloudFront distributions")
        disable_cloudfront(region)

        # Delete the stack
        print("\nStep 3: Deleting CloudFormation stack")
        run([
            "aws", "cloudformation", "delete-stack",
            "--stack-name", STACK_NAME, "--region", region,
        ], capture=True)
        print("  Waiting for stack deletion...")
        wait = run_quiet([
            "aws", "cloudformation", "wait", "stack-delete-complete",
            "--stack-name", STACK_NAME, "--region", region,
        ])
        if wait.returncode != 0:
            print("  Stack deletion may have failed. Check the CloudFormation console.")
            sys.exit(1)
        print("  Stack deleted")

    # Delete Lambda deployment bucket (not in the stack)
    print("\nStep 4: Cleaning up Lambda deployment bucket")
    delete_lambda_bucket(region, account_id)

    # Clean cadence.yaml
    print("\nStep 5: Cleaning cadence.yaml")
    clean_config(config_path)

    print("\n=================================")
    print("  Teardown complete!")
    print("\n  All AWS resources have been removed.")
    print("  To set up again, run:")
    print("    python3 scripts/setup.py")
    print("    python3 scripts/deploy.py")
    print("=================================")


if __name__ == "__main__":
    main()
