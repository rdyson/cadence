#!/usr/bin/env python3
"""
cadence validate
Checks that all AWS resources exist and are properly configured.
Usage: python scripts/validate.py [--config cadence.yaml]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

import yaml

try:
    import warnings
    warnings.filterwarnings("ignore", message=".*Boto3 will no longer support.*")
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    print("Error: boto3 is required. Run: pip install boto3")
    sys.exit(1)


class Validator:
    def __init__(self, config: dict):
        self.config = config
        self.aws = config.get("aws", {})
        self.region = self.aws.get("region", "eu-west-2")
        self.project_name = self.config.get("name", "Cadence").lower().replace(" ", "-")
        self.api_name = f"{self.project_name}-api"
        self.role_name = f"{self.project_name}-lambda-role"
        self.passed = 0
        self.failed = 0
        self.warnings = 0

    def ok(self, label: str, detail: str = ""):
        self.passed += 1
        suffix = f" — {detail}" if detail else ""
        print(f"  ✓ {label}{suffix}")

    def fail(self, label: str, detail: str = ""):
        self.failed += 1
        suffix = f" — {detail}" if detail else ""
        print(f"  ✗ {label}{suffix}")

    def warn(self, label: str, detail: str = ""):
        self.warnings += 1
        suffix = f" — {detail}" if detail else ""
        print(f"  ⚠ {label}{suffix}")

    def check_config(self):
        print("\n▶ Configuration")
        required = {
            "name": self.config.get("name"),
            "completion_date": self.config.get("completion_date"),
            "interval": self.config.get("interval"),
            "csv": self.config.get("csv"),
        }
        for key, val in required.items():
            if val:
                self.ok(key, str(val))
            else:
                self.fail(key, "missing from cadence.yaml")

        users = self.config.get("users", [])
        if users:
            self.ok(f"users ({len(users)})", ", ".join(u["name"] for u in users))
        else:
            self.fail("users", "no users defined")

        csv_path = Path(self.config.get("csv", "items.csv"))
        if csv_path.exists():
            self.ok(f"csv file", str(csv_path))
        else:
            self.fail(f"csv file", f"{csv_path} not found")

    def check_dynamodb(self):
        print("\n▶ DynamoDB")
        table_name = self.aws.get("dynamodb_table", "")
        if not table_name:
            self.fail("dynamodb_table", "not set in cadence.yaml")
            return

        try:
            ddb = boto3.client("dynamodb", region_name=self.region)
            desc = ddb.describe_table(TableName=table_name)
            status = desc["Table"]["TableStatus"]
            count = desc["Table"]["ItemCount"]
            if status == "ACTIVE":
                self.ok(f"table: {table_name}", f"status={status}, items={count}")
            else:
                self.warn(f"table: {table_name}", f"status={status}")
        except ClientError as e:
            self.fail(f"table: {table_name}", e.response["Error"]["Message"])

    def check_cognito(self):
        print("\n▶ Cognito")
        pool_id = self.aws.get("cognito_user_pool_id", "")
        client_id = self.aws.get("cognito_client_id", "")

        if not pool_id:
            self.fail("cognito_user_pool_id", "not set in cadence.yaml")
            return
        if not client_id:
            self.fail("cognito_client_id", "not set in cadence.yaml")
            return

        try:
            cog = boto3.client("cognito-idp", region_name=self.region)
            pool = cog.describe_user_pool(UserPoolId=pool_id)
            pool_name = pool["UserPool"]["Name"]
            self.ok(f"user pool: {pool_id}", pool_name)
        except ClientError as e:
            self.fail(f"user pool: {pool_id}", e.response["Error"]["Message"])
            return

        # Check app client
        try:
            client = cog.describe_user_pool_client(UserPoolId=pool_id, ClientId=client_id)
            self.ok(f"app client: {client_id}", client["UserPoolClient"]["ClientName"])
        except ClientError as e:
            self.fail(f"app client: {client_id}", e.response["Error"]["Message"])

        # Check users exist
        config_users = self.config.get("users", [])
        for user in config_users:
            email = user.get("email", "")
            try:
                cog.admin_get_user(UserPoolId=pool_id, Username=email)
                self.ok(f"user: {user['name']}", email)
            except ClientError:
                self.fail(f"user: {user['name']}", f"{email} not found in pool")

    def check_lambda(self):
        print("\n▶ Lambda")
        try:
            lam = boto3.client("lambda", region_name=self.region)
            fn = lam.get_function(FunctionName=self.api_name)
            config = fn["Configuration"]
            runtime = config["Runtime"]
            state = config["State"]
            table_env = config.get("Environment", {}).get("Variables", {}).get("DYNAMODB_TABLE", "")
            if state == "Active":
                self.ok(self.api_name, f"runtime={runtime}, state={state}")
            else:
                self.warn(self.api_name, f"state={state}")

            expected_table = self.aws.get("dynamodb_table", "")
            if table_env == expected_table:
                self.ok("DYNAMODB_TABLE env var", table_env)
            else:
                self.fail("DYNAMODB_TABLE env var", f"expected '{expected_table}', got '{table_env}'")
        except ClientError as e:
            self.fail(self.api_name, e.response["Error"]["Message"])

    def check_api_gateway(self):
        print("\n▶ API Gateway")
        api_url = self.aws.get("api_url", "")
        if not api_url:
            self.fail("api_url", "not set in cadence.yaml")
            return

        try:
            apigw = boto3.client("apigatewayv2", region_name=self.region)
            apis = apigw.get_apis()
            found = None
            for api in apis.get("Items", []):
                if api["Name"] == self.api_name:
                    found = api
                    break

            if found:
                self.ok(self.api_name, f"id={found['ApiId']}, protocol={found['ProtocolType']}")
                expected_url = f"https://{found['ApiId']}.execute-api.{self.region}.amazonaws.com"
                if api_url == expected_url:
                    self.ok("api_url matches", api_url)
                else:
                    self.warn("api_url mismatch", f"config={api_url}, actual={expected_url}")
            else:
                self.fail(self.api_name, "API not found")
        except ClientError as e:
            self.fail("API Gateway", e.response["Error"]["Message"])

        # Try hitting the API (should get 401 without auth)
        try:
            req = urllib.request.Request(f"{api_url}/state")
            urllib.request.urlopen(req, timeout=10)
            self.warn("GET /state", "returned 200 without auth — authorizer may be misconfigured")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self.ok("GET /state", "returns 401 without auth (correct)")
            else:
                self.warn("GET /state", f"returned {e.code}")
        except Exception as e:
            self.fail("GET /state", str(e))

    def check_s3(self):
        print("\n▶ S3")
        bucket = self.aws.get("s3_bucket", "")
        if not bucket:
            self.fail("s3_bucket", "not set in cadence.yaml")
            return

        try:
            s3 = boto3.client("s3", region_name=self.region)
            s3.head_bucket(Bucket=bucket)
            self.ok(f"bucket: {bucket}", "exists")
        except ClientError as e:
            self.fail(f"bucket: {bucket}", e.response["Error"]["Message"])
            return

        # Check required files
        expected_files = ["index.html", "app.js", "style.css", "cadence.json"]
        for key in expected_files:
            try:
                s3.head_object(Bucket=bucket, Key=key)
                self.ok(f"  {key}")
            except ClientError:
                self.fail(f"  {key}", "not found — run deploy.py")

    def check_cloudfront(self):
        print("\n▶ CloudFront")
        cf_url = self.aws.get("cloudfront_url", "")
        if not cf_url:
            self.warn("cloudfront_url", "not set — HTTPS won't work")
            return

        try:
            req = urllib.request.Request(cf_url)
            resp = urllib.request.urlopen(req, timeout=10)
            if resp.status == 200:
                self.ok(f"dashboard reachable", cf_url)
            else:
                self.warn(f"dashboard returned {resp.status}", cf_url)
        except Exception as e:
            self.fail(f"dashboard unreachable", f"{cf_url} — {e}")

    def check_iam(self):
        print("\n▶ IAM")
        try:
            iam = boto3.client("iam")
            iam.get_role(RoleName=self.role_name)
            self.ok(self.role_name, "exists")

            policies = iam.list_attached_role_policies(RoleName=self.role_name)
            for p in policies["AttachedPolicies"]:
                self.ok(f"  policy: {p['PolicyName']}")
        except ClientError as e:
            self.fail(self.role_name, e.response["Error"]["Message"])

    def run(self):
        print("================================")
        print("  Cadence — Validate")
        print(f"  Region: {self.region}")
        print("================================")

        try:
            self.check_config()
            self.check_dynamodb()
            self.check_cognito()
            self.check_lambda()
            self.check_api_gateway()
            self.check_s3()
            self.check_cloudfront()
            self.check_iam()
        except NoCredentialsError:
            self.fail("AWS credentials", "boto3 could not find credentials for this shell")
            print('  Hint: if `aws` CLI works, try:')
            print('    eval "$(aws configure export-credentials --format env)" && python3 scripts/validate.py')
            print("\n================================")
            print(f"  ✓ {self.passed} passed")
            print(f"  ✗ {self.failed} failed")
            if self.warnings:
                print(f"  ⚠ {self.warnings} warnings")
            print("================================")
            return 1

        print("\n================================")
        print(f"  ✓ {self.passed} passed")
        if self.warnings:
            print(f"  ⚠ {self.warnings} warnings")
        if self.failed:
            print(f"  ✗ {self.failed} failed")
        print("================================\n")

        return 0 if self.failed == 0 else 1


def main():
    parser = argparse.ArgumentParser(description="Validate Cadence AWS deployment")
    parser.add_argument("--config", "-c", default="cadence.yaml")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: {args.config} not found")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    validator = Validator(config)
    sys.exit(validator.run())


if __name__ == "__main__":
    main()
