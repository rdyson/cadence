#!/usr/bin/env python3
"""
cadence — CloudFormation-based AWS setup
Replaces setup-aws.sh, setup-cloudfront.sh, and setup-otp.sh with a single
CloudFormation stack plus post-deploy user provisioning.

Usage: python3 scripts/setup.py [--config cadence.yaml]
"""

from __future__ import annotations

import json
import os
import secrets
import string
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = ROOT / "infrastructure" / "template.yaml"
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


def ensure_lambda_bucket(region: str, account_id: str) -> str:
    """Create or find the S3 bucket for Lambda deployment packages."""
    bucket = f"cadence-lambda-{account_id}-{region}"
    check = run_quiet(["aws", "s3api", "head-bucket", "--bucket", bucket, "--region", region])
    if check.returncode != 0:
        print(f"  Creating Lambda deployment bucket: {bucket}")
        if region == "us-east-1":
            run(["aws", "s3api", "create-bucket", "--bucket", bucket], capture=True)
        else:
            run([
                "aws", "s3api", "create-bucket", "--bucket", bucket,
                "--create-bucket-configuration", f"LocationConstraint={region}",
            ], capture=True)
    else:
        print(f"  Lambda deployment bucket exists: {bucket}")
    return bucket


def upload_lambda_code(bucket: str, region: str) -> tuple[str, str]:
    """Zip and upload Lambda code to S3. Returns (api_key, auth_key)."""
    timestamp = str(int(time.time()))

    # Main Lambda
    api_key = f"lambda-api-{timestamp}.zip"
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        api_zip = tmp.name
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(ROOT / "backend" / "lambda_function.py", "lambda_function.py")
    run(["aws", "s3", "cp", api_zip, f"s3://{bucket}/{api_key}", "--region", region], capture=True)
    Path(api_zip).unlink()
    print(f"  Uploaded {api_key}")

    # Auth triggers Lambda
    auth_key = f"lambda-auth-{timestamp}.zip"
    auth_file = ROOT / "backend" / "auth_triggers.py"
    if auth_file.exists():
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            auth_zip = tmp.name
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(auth_file, "auth_triggers.py")
        run(["aws", "s3", "cp", auth_zip, f"s3://{bucket}/{auth_key}", "--region", region], capture=True)
        Path(auth_zip).unlink()
        print(f"  Uploaded {auth_key}")

    return api_key, auth_key


def deploy_stack(config: dict, region: str, lambda_bucket: str, api_key: str, auth_key: str) -> dict:
    """Deploy or update the CloudFormation stack. Returns stack outputs."""
    aws_cfg = config.get("aws", {})
    project_name = config.get("name", "Cadence").lower().replace(" ", "-")
    table_name = aws_cfg.get("dynamodb_table", "cadence")
    otp = str(config.get("otp", False)).lower()
    ses_sender = config.get("ses_sender_email", "")

    custom_domain = aws_cfg.get("custom_domain", "")
    acm_cert_arn = aws_cfg.get("acm_certificate_arn", "")

    params = [
        f"ProjectName={project_name}",
        f"DynamoDBTableName={table_name}",
        f"EnableOtp={otp}",
        f"SesSenderEmail={ses_sender}",
        f"LambdaS3Bucket={lambda_bucket}",
        f"LambdaS3Key={api_key}",
        f"AuthTriggersS3Key={auth_key}",
        f"CustomDomain={custom_domain}",
        f"AcmCertificateArn={acm_cert_arn}",
    ]

    # Check if stack exists
    check = run_quiet([
        "aws", "cloudformation", "describe-stacks",
        "--stack-name", STACK_NAME, "--region", region,
    ])
    stack_exists = check.returncode == 0

    action = "update-stack" if stack_exists else "create-stack"
    verb = "Updating" if stack_exists else "Creating"
    print(f"\n  {verb} CloudFormation stack: {STACK_NAME}")

    cmd = [
        "aws", "cloudformation", action,
        "--stack-name", STACK_NAME,
        "--template-body", f"file://{TEMPLATE_PATH}",
        "--parameters", *[f"ParameterKey={p.split('=')[0]},ParameterValue={p.split('=', 1)[1]}" for p in params],
        "--capabilities", "CAPABILITY_NAMED_IAM",
        "--region", region,
    ]
    result = run_quiet(cmd)

    if result.returncode != 0:
        if "No updates are to be performed" in result.stderr:
            print("  Stack is already up to date")
        else:
            print(f"  Error: {result.stderr.strip()}")
            sys.exit(1)
    else:
        wait_event = "stack-update-complete" if stack_exists else "stack-create-complete"
        print(f"  Waiting for stack {wait_event.replace('-', ' ')}...")
        wait_result = run_quiet([
            "aws", "cloudformation", "wait", wait_event,
            "--stack-name", STACK_NAME, "--region", region,
        ])
        if wait_result.returncode != 0:
            print(f"  Stack operation failed. Check the CloudFormation console for details.")
            # Print recent events for debugging
            run(["aws", "cloudformation", "describe-stack-events",
                 "--stack-name", STACK_NAME, "--region", region,
                 "--query", "StackEvents[?ResourceStatus=='CREATE_FAILED' || ResourceStatus=='UPDATE_FAILED'].[LogicalResourceId,ResourceStatusReason]",
                 "--output", "table"], capture=False)
            sys.exit(1)
        print("  Stack operation complete")

    # Get outputs
    result = run([
        "aws", "cloudformation", "describe-stacks",
        "--stack-name", STACK_NAME, "--region", region,
        "--query", "Stacks[0].Outputs",
        "--output", "json",
    ], capture=True)

    outputs = {}
    for item in json.loads(result.stdout):
        outputs[item["OutputKey"]] = item["OutputValue"]

    return outputs


def create_cognito_users(config: dict, pool_id: str, region: str) -> None:
    """Create Cognito users from cadence.yaml. Prints temp passwords for new users."""
    users = config.get("users", [])
    if not users:
        return

    print("\n  Creating Cognito users...")
    for user in users:
        email = user.get("email", "")
        name = user.get("name", user.get("id", ""))
        if not email:
            continue

        check = run_quiet([
            "aws", "cognito-idp", "admin-get-user",
            "--user-pool-id", pool_id, "--username", email, "--region", region,
        ])
        if check.returncode == 0:
            print(f"  User already exists: {name} ({email})")
            continue

        temp_pw = secrets.token_urlsafe(12) + "!A1a"
        run([
            "aws", "cognito-idp", "admin-create-user",
            "--user-pool-id", pool_id, "--username", email,
            "--user-attributes", f"Name=email,Value={email}", "Name=email_verified,Value=true",
            "--temporary-password", temp_pw,
            "--message-action", "SUPPRESS",
            "--region", region,
        ], capture=True)
        print(f"  Created user: {name} ({email}) -- temp password: {temp_pw}")


def setup_ses(config: dict, region: str) -> None:
    """Verify SES sender and recipient emails if OTP is enabled."""
    if not config.get("otp"):
        return

    ses_sender = config.get("ses_sender_email", "")
    if not ses_sender:
        print("\n  Warning: otp is enabled but ses_sender_email is not set in cadence.yaml")
        return

    print("\n  Verifying SES sender email...")
    status = run_quiet([
        "aws", "ses", "get-identity-verification-attributes",
        "--identities", ses_sender, "--region", region,
        "--query", f'VerificationAttributes."{ses_sender}".VerificationStatus',
        "--output", "text",
    ])
    if status.stdout.strip() == "Success":
        print(f"  {ses_sender} already verified")
    else:
        run(["aws", "ses", "verify-email-identity", "--email-address", ses_sender, "--region", region], capture=True)
        print(f"  Verification email sent to {ses_sender}")
        print("  Check your inbox and click the verification link.")

    # Check sandbox mode
    quota = run_quiet([
        "aws", "ses", "get-send-quota", "--region", region,
        "--query", "Max24HourSend", "--output", "text",
    ])
    try:
        max_send = float(quota.stdout.strip())
    except (ValueError, AttributeError):
        max_send = 0

    if max_send <= 200:
        print("\n  SES is in sandbox mode. Verifying user emails...")
        for user in config.get("users", []):
            email = user.get("email", "")
            if not email:
                continue
            check = run_quiet([
                "aws", "ses", "get-identity-verification-attributes",
                "--identities", email, "--region", region,
                "--query", f'VerificationAttributes."{email}".VerificationStatus',
                "--output", "text",
            ])
            if check.stdout.strip() == "Success":
                print(f"  {email} already verified")
            else:
                run(["aws", "ses", "verify-email-identity", "--email-address", email, "--region", region], capture=True)
                print(f"  Verification email sent to {email}")
        print("  Each user must click their SES verification link before OTP will work.")
    else:
        print("  SES is in production mode -- no recipient verification needed")


def update_config(config_path: Path, config: dict, outputs: dict) -> None:
    """Write stack outputs back to cadence.yaml."""
    config.setdefault("aws", {})
    config["aws"]["cognito_user_pool_id"] = outputs["CognitoUserPoolId"]
    config["aws"]["cognito_client_id"] = outputs["CognitoClientId"]
    config["aws"]["api_url"] = outputs["ApiUrl"]
    config["aws"]["s3_bucket"] = outputs["S3BucketName"]
    config["aws"]["cloudfront_url"] = outputs["CloudFrontUrl"]
    config["aws"]["cloudfront_distribution_id"] = outputs["CloudFrontDistributionId"]
    with open(config_path, "w") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    print("\n  cadence.yaml updated with stack outputs")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Deploy Cadence AWS infrastructure via CloudFormation")
    parser.add_argument("--config", "-c", default="cadence.yaml")
    args = parser.parse_args()

    os.chdir(ROOT)
    config_path = Path(args.config)
    config = load_config(config_path)
    aws_cfg = config.get("aws", {})
    region = aws_cfg.get("region", "eu-west-2")
    account_id = get_account_id()

    print("=================================")
    print("  Cadence CloudFormation Setup")
    print(f"  Region:  {region}")
    print(f"  Account: {account_id}")
    print("=================================\n")

    # Step 1: Upload Lambda code
    print("Step 1: Uploading Lambda code to S3")
    lambda_bucket = ensure_lambda_bucket(region, account_id)
    api_key, auth_key = upload_lambda_code(lambda_bucket, region)

    # Step 2: Deploy CloudFormation stack
    print("\nStep 2: Deploying CloudFormation stack")
    outputs = deploy_stack(config, region, lambda_bucket, api_key, auth_key)

    print("\n  Stack outputs:")
    for k, v in outputs.items():
        print(f"    {k}: {v}")

    # Step 3: Create Cognito users
    print("\nStep 3: Creating Cognito users")
    create_cognito_users(config, outputs["CognitoUserPoolId"], region)

    # Step 4: SES verification (if OTP)
    if config.get("otp"):
        print("\nStep 4: SES email verification")
        setup_ses(config, region)

    # Step 5: Write outputs to cadence.yaml
    print("\nStep 5: Updating cadence.yaml")
    update_config(config_path, config, outputs)

    print("\n=================================")
    print("  Setup complete!")
    print(f"\n  CloudFront: {outputs['CloudFrontUrl']}")
    print(f"  API:        {outputs['ApiUrl']}")
    print(f"  S3 Bucket:  {outputs['S3BucketName']}")
    print("\n  Next steps:")
    print("    python3 scripts/deploy.py")
    print("\n  Temp passwords (if any) were printed above.")
    print("  Share them with each user for their first sign-in.")
    print("=================================")


if __name__ == "__main__":
    main()
