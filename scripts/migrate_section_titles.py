#!/usr/bin/env python3
"""
One-time migration: rename item titles in DynamoDB state to include section numbers.
Run BEFORE deploying the updated items.csv.

Usage: python scripts/migrate_section_titles.py [--dry-run]
"""

import argparse
import warnings

warnings.filterwarnings("ignore", message=".*Boto3 will no longer support.*")
import boto3
import yaml

# Old title -> New title mapping (only Udemy sections that changed)
RENAMES = {
    "Introduction - AWS Certified Solutions Architect Associate": "Section 1: Introduction - AWS Certified Solutions Architect Associate",
    "Code & Slides Download": "Section 2: Code & Slides Download",
    "Getting started with AWS": "Section 3: Getting started with AWS",
    "IAM & AWS CLI": "Section 4: IAM & AWS CLI",
    "EC2 Fundamentals": "Section 5: EC2 Fundamentals",
    "EC2 - Solutions Architect Associate Level": "Section 6: EC2 - Solutions Architect Associate Level",
    "EC2 Instance Storage": "Section 7: EC2 Instance Storage",
    "High Availability and Scalability: ELB & ASG": "Section 8: High Availability and Scalability: ELB & ASG",
    "AWS Fundamentals: RDS + Aurora + ElastiCache": "Section 9: AWS Fundamentals: RDS + Aurora + ElastiCache",
    "Route 53": "Section 10: Route 53",
    "Classic Solutions Architecture Discussions": "Section 11: Classic Solutions Architecture Discussions",
    "Amazon S3 Introduction": "Section 12: Amazon S3 Introduction",
    "Advanced Amazon S3": "Section 13: Advanced Amazon S3",
    "Amazon S3 Security": "Section 14: Amazon S3 Security",
    "CloudFront & AWS Global Accelerator": "Section 15: CloudFront & AWS Global Accelerator",
    "AWS Storage Extras": "Section 16: AWS Storage Extras",
    "Decoupling applications: SQS, SNS, Kinesis, Active MQ": "Section 17: Decoupling applications: SQS, SNS, Kinesis, Active MQ",
    "Containers on AWS: ECS, Fargate, ECR & EKS": "Section 18: Containers on AWS: ECS, Fargate, ECR & EKS",
    "Serverless Overviews from a Solution Architect Perspective": "Section 19: Serverless Overviews from a Solution Architect Perspective",
    "Serverless Solution Architecture Discussions": "Section 20: Serverless Solution Architecture Discussions",
    "Databases in AWS": "Section 21: Databases in AWS",
    "Data & Analytics": "Section 22: Data & Analytics",
    "Machine Learning": "Section 23: Machine Learning",
    "AWS Monitoring & Audit: CloudWatch, CloudTrail & Config": "Section 24: AWS Monitoring & Audit: CloudWatch, CloudTrail & Config",
    "Identity and Access Management (IAM) - Advanced": "Section 25: Identity and Access Management (IAM) - Advanced",
    "AWS Security & Encryption: KMS, SSM Parameter Store, Shield, WAF": "Section 26: AWS Security & Encryption: KMS, SSM Parameter Store, Shield, WAF",
    "Networking - VPC": "Section 27: Networking - VPC",
    "Disaster Recovery & Migrations": "Section 28: Disaster Recovery & Migrations",
    "More Solution Architectures": "Section 29: More Solution Architectures",
    "Other Services": "Section 30: Other Services",
    "WhitePapers and Architectures - AWS Certified Solutions Architect Associate": "Section 31: WhitePapers and Architectures - AWS Certified Solutions Architect Associate",
}


def migrate(dry_run: bool = False):
    with open("cadence.yaml") as f:
        config = yaml.safe_load(f)

    aws = config.get("aws", {})
    region = aws.get("region", "eu-west-2")
    table_name = aws.get("dynamodb_table", "cadence")

    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)

    result = table.scan()
    items = result.get("Items", [])

    for item in items:
        user_id = item.get("userId")
        checks = item.get("checks", {})
        updated = False

        new_checks = {}
        for key, value in checks.items():
            if key in RENAMES:
                new_key = RENAMES[key]
                print(f"  {user_id}: '{key}' -> '{new_key}'")
                new_checks[new_key] = value
                updated = True
            else:
                new_checks[key] = value

        if updated:
            if dry_run:
                print(f"  [DRY RUN] Would update {user_id}")
            else:
                table.put_item(Item={
                    **item,
                    "checks": new_checks,
                })
                print(f"  Updated {user_id}")
        else:
            print(f"  {user_id}: no changes needed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate DynamoDB state to section-prefixed titles")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
