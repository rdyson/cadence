#!/bin/bash
# cadence — tear down all AWS infrastructure
# Removes everything created by setup-aws.sh and setup-cloudfront.sh.
# Usage: bash scripts/teardown-aws.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."
CONFIG="$ROOT/cadence.yaml"

if [ ! -f "$CONFIG" ]; then
    echo "Error: cadence.yaml not found."
    exit 1
fi

cd "$ROOT"

# Parse config
_CADENCE_ENV=$(mktemp)
python3 - <<'PYEOF' > "$_CADENCE_ENV"
import yaml
with open("cadence.yaml") as f:
    c = yaml.safe_load(f)
aws = c.get("aws", {})
print(f'REGION="{aws.get("region", "eu-west-2")}"')
print(f'TABLE_NAME="{aws.get("dynamodb_table", "cadence-study")}"')
print(f'POOL_ID="{aws.get("cognito_user_pool_id", "")}"')
print(f'BUCKET="{aws.get("s3_bucket", "")}"')
print(f'CF_URL="{aws.get("cloudfront_url", "")}"')
print(f'PROJECT_NAME="{c.get("name", "Cadence").lower().replace(" ", "-")}"')
PYEOF
source "$_CADENCE_ENV"
rm -f "$_CADENCE_ENV"

LAMBDA_ROLE="cadence-lambda-role"
LAMBDA_NAME="cadence-api"
API_NAME="cadence-api"

echo "================================="
echo "  Cadence AWS Teardown"
echo "  Region:  $REGION"
echo "  Bucket:  $BUCKET"
echo "================================="
echo ""
echo "⚠  This will permanently delete ALL Cadence AWS resources."
echo "   DynamoDB data, Cognito users, and all infrastructure will be lost."
echo ""
read -p "Type 'destroy' to confirm: " CONFIRM
if [ "$CONFIRM" != "destroy" ]; then
    echo "Aborted."
    exit 1
fi
echo ""

# ── Step 1: CloudFront ────────────────────────────────────────────────────────
echo "▶ Step 1: Removing CloudFront distribution..."

DIST_ID=$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?Origins.Items[0].Id=='${BUCKET}'].Id" \
    --output text 2>/dev/null || echo "")

if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then
    # Disable first
    ETAG=$(aws cloudfront get-distribution-config --id "$DIST_ID" --query 'ETag' --output text)
    STATUS=$(aws cloudfront get-distribution --id "$DIST_ID" --query 'Distribution.DistributionConfig.Enabled' --output text)

    if [ "$STATUS" = "true" ] || [ "$STATUS" = "True" ]; then
        echo "  Disabling distribution $DIST_ID..."
        aws cloudfront get-distribution-config --id "$DIST_ID" --query 'DistributionConfig' --output json \
            | python3 -c "import json,sys; c=json.load(sys.stdin); c['Enabled']=False; print(json.dumps(c))" \
            | aws cloudfront update-distribution --id "$DIST_ID" --if-match "$ETAG" --distribution-config file:///dev/stdin > /dev/null
        echo "  Waiting for distribution to disable (~5 minutes)..."
        aws cloudfront wait distribution-deployed --id "$DIST_ID"
    fi

    # Delete
    ETAG=$(aws cloudfront get-distribution-config --id "$DIST_ID" --query 'ETag' --output text)
    aws cloudfront delete-distribution --id "$DIST_ID" --if-match "$ETAG"
    echo "  ✓ Distribution deleted"
else
    echo "  ✓ No distribution found — skipping"
fi

# Delete OAC
OAC_ID=$(aws cloudfront list-origin-access-controls \
    --query "OriginAccessControlList.Items[?Name=='cadence-oac'].Id" --output text 2>/dev/null || echo "")

if [ -n "$OAC_ID" ] && [ "$OAC_ID" != "None" ]; then
    ETAG=$(aws cloudfront get-origin-access-control --id "$OAC_ID" --query 'ETag' --output text)
    aws cloudfront delete-origin-access-control --id "$OAC_ID" --if-match "$ETAG"
    echo "  ✓ Origin Access Control deleted"
fi

# ── Step 2: S3 bucket ─────────────────────────────────────────────────────────
echo ""
echo "▶ Step 2: Removing S3 bucket..."

if [ -n "$BUCKET" ] && aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
    aws s3 rm "s3://${BUCKET}" --recursive --region "$REGION" > /dev/null
    aws s3api delete-bucket --bucket "$BUCKET" --region "$REGION"
    echo "  ✓ Bucket deleted: $BUCKET"
else
    echo "  ✓ Bucket not found — skipping"
fi

# ── Step 3: Cognito ───────────────────────────────────────────────────────────
echo ""
echo "▶ Step 3: Removing Cognito user pool..."

if [ -n "$POOL_ID" ]; then
    # Must delete the domain first if one exists
    DOMAIN=$(aws cognito-idp describe-user-pool --user-pool-id "$POOL_ID" --region "$REGION" \
        --query 'UserPool.Domain' --output text 2>/dev/null || echo "")
    if [ -n "$DOMAIN" ] && [ "$DOMAIN" != "None" ]; then
        aws cognito-idp delete-user-pool-domain --domain "$DOMAIN" --user-pool-id "$POOL_ID" --region "$REGION" 2>/dev/null || true
    fi

    aws cognito-idp delete-user-pool --user-pool-id "$POOL_ID" --region "$REGION" 2>/dev/null && \
        echo "  ✓ User pool deleted: $POOL_ID" || \
        echo "  ✓ User pool not found — skipping"
else
    echo "  ✓ No user pool configured — skipping"
fi

# ── Step 4: API Gateway ───────────────────────────────────────────────────────
echo ""
echo "▶ Step 4: Removing API Gateway..."

API_ID=$(aws apigatewayv2 get-apis --region "$REGION" \
    --query "Items[?Name=='$API_NAME'].ApiId" --output text 2>/dev/null || echo "")

if [ -n "$API_ID" ] && [ "$API_ID" != "None" ]; then
    aws apigatewayv2 delete-api --api-id "$API_ID" --region "$REGION"
    echo "  ✓ API deleted: $API_ID"
else
    echo "  ✓ API not found — skipping"
fi

# ── Step 5: Lambda ────────────────────────────────────────────────────────────
echo ""
echo "▶ Step 5: Removing Lambda function..."

if aws lambda get-function --function-name "$LAMBDA_NAME" --region "$REGION" &>/dev/null; then
    aws lambda delete-function --function-name "$LAMBDA_NAME" --region "$REGION"
    echo "  ✓ Lambda deleted: $LAMBDA_NAME"
else
    echo "  ✓ Lambda not found — skipping"
fi

# ── Step 6: IAM role ──────────────────────────────────────────────────────────
echo ""
echo "▶ Step 6: Removing IAM role..."

if aws iam get-role --role-name "$LAMBDA_ROLE" &>/dev/null; then
    # Detach all policies
    POLICIES=$(aws iam list-attached-role-policies --role-name "$LAMBDA_ROLE" \
        --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null || echo "")
    for POLICY_ARN in $POLICIES; do
        aws iam detach-role-policy --role-name "$LAMBDA_ROLE" --policy-arn "$POLICY_ARN"
    done
    aws iam delete-role --role-name "$LAMBDA_ROLE"
    echo "  ✓ Role deleted: $LAMBDA_ROLE"
else
    echo "  ✓ Role not found — skipping"
fi

# ── Step 7: DynamoDB ──────────────────────────────────────────────────────────
echo ""
echo "▶ Step 7: Removing DynamoDB table..."

if aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$REGION" &>/dev/null; then
    aws dynamodb delete-table --table-name "$TABLE_NAME" --region "$REGION" > /dev/null
    echo "  ✓ Table deleted: $TABLE_NAME"
else
    echo "  ✓ Table not found — skipping"
fi

# ── Step 8: Clean up cadence.yaml ─────────────────────────────────────────────
echo ""
echo "▶ Step 8: Cleaning up cadence.yaml..."

python3 - <<'PYEOF'
import yaml
with open("cadence.yaml") as f:
    config = yaml.safe_load(f)
aws = config.get("aws", {})
for key in ["cognito_user_pool_id", "cognito_client_id", "api_url", "s3_bucket", "cloudfront_url"]:
    aws.pop(key, None)
config["aws"] = aws
with open("cadence.yaml", "w") as f:
    yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
print("  ✓ Removed generated values from cadence.yaml")
PYEOF

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "================================="
echo "  ✅ Teardown complete!"
echo ""
echo "  All AWS resources have been removed."
echo "  To set up again, run:"
echo "    bash scripts/setup-aws.sh"
echo "    bash scripts/setup-cloudfront.sh"
echo "    python scripts/deploy.py"
echo "================================="
