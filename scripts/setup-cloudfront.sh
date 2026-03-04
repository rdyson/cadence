#!/bin/bash
# cadence — CloudFront distribution setup
# Run once after setup-aws.sh has created the S3 bucket.
# Usage: bash scripts/setup-cloudfront.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."

if [ ! -f "$ROOT/cadence.yaml" ]; then
    echo "Error: cadence.yaml not found. Run setup-aws.sh first."
    exit 1
fi

# Parse config
_CADENCE_ENV=$(mktemp)
python3 - <<'PYEOF' > "$_CADENCE_ENV"
import yaml
with open("cadence.yaml") as f:
    c = yaml.safe_load(f)
aws = c.get("aws", {})
print(f'REGION="{aws.get("region", "eu-west-2")}"')
print(f'BUCKET="{aws.get("s3_bucket", "")}"')
print(f'EXISTING_CF_URL="{aws.get("cloudfront_url", "")}"')
PYEOF
source "$_CADENCE_ENV"
rm -f "$_CADENCE_ENV"

if [ -z "$BUCKET" ]; then
    echo "Error: aws.s3_bucket not set in cadence.yaml — run setup-aws.sh first."
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "================================="
echo "  Cadence CloudFront Setup"
echo "  Bucket: $BUCKET"
echo "  Region: $REGION"
echo "================================="
echo ""

# Check if CloudFront already configured
if [ -n "$EXISTING_CF_URL" ]; then
    echo "  ✓ CloudFront already configured: $EXISTING_CF_URL"
    echo "  To recreate, remove aws.cloudfront_url from cadence.yaml and re-run."
    exit 0
fi

# ── Create Origin Access Control ──────────────────────────────────────────────
echo "▶ Creating Origin Access Control (OAC)..."

OAC_ID=$(aws cloudfront list-origin-access-controls \
    --query "OriginAccessControlList.Items[?Name=='cadence-oac'].Id" \
    --output text 2>/dev/null || echo "")

if [ -z "$OAC_ID" ] || [ "$OAC_ID" = "None" ]; then
    OAC_ID=$(aws cloudfront create-origin-access-control \
        --origin-access-control-config '{
            "Name": "cadence-oac",
            "Description": "Cadence S3 OAC",
            "SigningProtocol": "sigv4",
            "SigningBehavior": "always",
            "OriginAccessControlOriginType": "s3"
        }' \
        --query 'OriginAccessControl.Id' --output text)
    echo "  ✓ OAC created: $OAC_ID"
else
    echo "  ✓ OAC already exists: $OAC_ID"
fi

# ── Create CloudFront distribution ────────────────────────────────────────────
echo ""
echo "▶ Creating CloudFront distribution..."
echo "  (This takes 3-5 minutes to deploy globally)"

DISTRIBUTION_CONFIG=$(cat <<EOF
{
    "CallerReference": "cadence-${BUCKET}",
    "Comment": "Cadence dashboard - ${BUCKET}",
    "DefaultCacheBehavior": {
        "TargetOriginId": "${BUCKET}",
        "ViewerProtocolPolicy": "redirect-to-https",
        "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
        "AllowedMethods": {
            "Quantity": 2,
            "Items": ["HEAD", "GET"],
            "CachedMethods": { "Quantity": 2, "Items": ["HEAD", "GET"] }
        },
        "Compress": true
    },
    "Origins": {
        "Quantity": 1,
        "Items": [{
            "Id": "${BUCKET}",
            "DomainName": "${BUCKET}.s3.${REGION}.amazonaws.com",
            "S3OriginConfig": { "OriginAccessIdentity": "" },
            "OriginAccessControlId": "${OAC_ID}"
        }]
    },
    "DefaultRootObject": "index.html",
    "CustomErrorResponses": {
        "Quantity": 1,
        "Items": [{
            "ErrorCode": 403,
            "ResponsePagePath": "/index.html",
            "ResponseCode": "200",
            "ErrorCachingMinTTL": 0
        }]
    },
    "Enabled": true,
    "HttpVersion": "http2",
    "PriceClass": "PriceClass_100"
}
EOF
)

DIST_RESULT=$(aws cloudfront create-distribution \
    --distribution-config "$DISTRIBUTION_CONFIG")

DIST_ID=$(echo "$DIST_RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['Distribution']['Id'])")
CF_DOMAIN=$(echo "$DIST_RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['Distribution']['DomainName'])")
CF_URL="https://${CF_DOMAIN}"

echo "  ✓ Distribution created: $DIST_ID"

# ── Update S3 bucket policy to allow CloudFront OAC ──────────────────────────
echo ""
echo "▶ Updating S3 bucket policy for CloudFront access..."

BUCKET_POLICY=$(cat <<EOF
{
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "AllowCloudFrontOAC",
        "Effect": "Allow",
        "Principal": { "Service": "cloudfront.amazonaws.com" },
        "Action": "s3:GetObject",
        "Resource": "arn:aws:s3:::${BUCKET}/*",
        "Condition": {
            "StringEquals": {
                "AWS:SourceArn": "arn:aws:cloudfront::${ACCOUNT_ID}:distribution/${DIST_ID}"
            }
        }
    }]
}
EOF
)

aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$BUCKET_POLICY"
echo "  ✓ Bucket policy updated"

# ── Write CloudFront URL back to cadence.yaml ─────────────────────────────────
echo ""
echo "▶ Updating cadence.yaml..."

python3 - "$CF_URL" <<'PYEOF'
import yaml, sys
cf_url = sys.argv[1]
with open("cadence.yaml") as f:
    config = yaml.safe_load(f)
config.setdefault("aws", {})
config["aws"]["cloudfront_url"] = cf_url
with open("cadence.yaml", "w") as f:
    yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
print("  ✓ cadence.yaml updated")
PYEOF

# ── Wait for deployment ───────────────────────────────────────────────────────
echo ""
echo "▶ Waiting for CloudFront distribution to deploy..."
echo "  (Checking every 30s — usually takes 3-5 minutes)"

while true; do
    STATUS=$(aws cloudfront get-distribution --id "$DIST_ID" \
        --query 'Distribution.Status' --output text)
    if [ "$STATUS" = "Deployed" ]; then
        break
    fi
    echo "  Status: $STATUS — waiting..."
    sleep 30
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "================================="
echo "  ✅ CloudFront setup complete!"
echo ""
echo "  Distribution: $DIST_ID"
echo "  Dashboard URL: $CF_URL"
echo ""
echo "  Next step:"
echo "  python scripts/deploy.py"
echo "================================="
