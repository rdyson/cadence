#!/bin/bash
# cadence — one-time AWS infrastructure setup
# Run once after configuring cadence.yaml.
# Prerequisites: aws CLI configured, python3 + pyyaml installed.
# Usage: bash scripts/setup-aws.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."
CONFIG="$ROOT/cadence.yaml"

if [ ! -f "$CONFIG" ]; then
    echo "Error: cadence.yaml not found. Copy cadence.example.yaml first."
    exit 1
fi

# Preflight checks
cd "$ROOT"
source "$SCRIPT_DIR/preflight.sh"

# Parse config with Python
_CADENCE_ENV=$(mktemp)
python3 - <<'PYEOF' > "$_CADENCE_ENV"
import yaml
with open("cadence.yaml") as f:
    c = yaml.safe_load(f)
aws = c.get("aws", {})
print(f'REGION="{aws.get("region", "eu-west-2")}"')
print(f'TABLE_NAME="{aws.get("dynamodb_table", "cadence-study")}"')
print(f'PROJECT_NAME="{c.get("name", "Cadence").lower().replace(" ", "-")}"')
PYEOF
source "$_CADENCE_ENV"
rm -f "$_CADENCE_ENV"

LAMBDA_ROLE="cadence-lambda-role"
LAMBDA_NAME="cadence-api"
API_NAME="cadence-api"
POOL_NAME="cadence-users"
CLIENT_NAME="cadence-web"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "================================="
echo "  Cadence AWS Setup"
echo "  Project: $PROJECT_NAME"
echo "  Region:  $REGION"
echo "  Account: $ACCOUNT_ID"
echo "================================="
echo ""

# ── Step 1: DynamoDB ──────────────────────────────────────────────────────────
echo "▶ Step 1: Creating DynamoDB table: $TABLE_NAME"
if aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$REGION" &>/dev/null; then
    echo "  ✓ Table already exists"
else
    aws dynamodb create-table \
        --table-name "$TABLE_NAME" \
        --attribute-definitions AttributeName=userId,AttributeType=S \
        --key-schema AttributeName=userId,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST \
        --region "$REGION"
    echo "  ✓ Table created"
fi

# ── Step 2: IAM Role ──────────────────────────────────────────────────────────
echo ""
echo "▶ Step 2: Creating Lambda IAM role: $LAMBDA_ROLE"

TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}'

if aws iam get-role --role-name "$LAMBDA_ROLE" &>/dev/null; then
    echo "  ✓ Role already exists"
else
    aws iam create-role \
        --role-name "$LAMBDA_ROLE" \
        --assume-role-policy-document "$TRUST_POLICY"

    aws iam attach-role-policy \
        --role-name "$LAMBDA_ROLE" \
        --policy-arn arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess

    aws iam attach-role-policy \
        --role-name "$LAMBDA_ROLE" \
        --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

    echo "  ✓ Role created"
    echo "  Waiting for role propagation..."
    sleep 10
fi

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE}"

# ── Step 3: Lambda ────────────────────────────────────────────────────────────
echo ""
echo "▶ Step 3: Deploying Lambda: $LAMBDA_NAME"

cd "$ROOT/backend"
zip -q lambda.zip lambda_function.py
cd "$ROOT"

if aws lambda get-function --function-name "$LAMBDA_NAME" --region "$REGION" &>/dev/null; then
    aws lambda update-function-code \
        --function-name "$LAMBDA_NAME" \
        --zip-file fileb://backend/lambda.zip \
        --region "$REGION" > /dev/null
    echo "  ✓ Lambda updated"
else
    aws lambda create-function \
        --function-name "$LAMBDA_NAME" \
        --runtime python3.12 \
        --handler lambda_function.handler \
        --role "$ROLE_ARN" \
        --zip-file fileb://backend/lambda.zip \
        --environment "Variables={DYNAMODB_TABLE=$TABLE_NAME}" \
        --timeout 15 \
        --region "$REGION" > /dev/null
    echo "  ✓ Lambda created"
fi

LAMBDA_ARN=$(aws lambda get-function --function-name "$LAMBDA_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text)

# ── Step 4: API Gateway ───────────────────────────────────────────────────────
echo ""
echo "▶ Step 4: Creating API Gateway (HTTP API)"

EXISTING_API=$(aws apigatewayv2 get-apis --region "$REGION" --query "Items[?Name=='$API_NAME'].ApiId" --output text)

if [ -n "$EXISTING_API" ]; then
    API_ID="$EXISTING_API"
    echo "  ✓ API already exists: $API_ID"
else
    API_ID=$(aws apigatewayv2 create-api \
        --name "$API_NAME" \
        --protocol-type HTTP \
        --cors-configuration '{"AllowOrigins":["*"],"AllowMethods":["GET","POST","OPTIONS"],"AllowHeaders":["Authorization","Content-Type"]}' \
        --region "$REGION" \
        --query ApiId --output text)
    echo "  ✓ API created: $API_ID"
fi

API_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com"

# Lambda integration
INTEGRATION_ID=$(aws apigatewayv2 get-integrations --api-id "$API_ID" --region "$REGION" --query "Items[0].IntegrationId" --output text 2>/dev/null || echo "")

if [ "$INTEGRATION_ID" = "None" ] || [ -z "$INTEGRATION_ID" ]; then
    INTEGRATION_ID=$(aws apigatewayv2 create-integration \
        --api-id "$API_ID" \
        --integration-type AWS_PROXY \
        --integration-uri "$LAMBDA_ARN" \
        --payload-format-version 2.0 \
        --region "$REGION" \
        --query IntegrationId --output text)
    echo "  ✓ Lambda integration created"
fi

# Add Lambda permission
aws lambda add-permission \
    --function-name "$LAMBDA_NAME" \
    --statement-id apigw-invoke \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*" \
    --region "$REGION" 2>/dev/null || true

# ── Step 5: Cognito ───────────────────────────────────────────────────────────
echo ""
echo "▶ Step 5: Creating Cognito User Pool: $POOL_NAME"

POOL_ID=$(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
    --query "UserPools[?Name=='$POOL_NAME'].Id" --output text)

if [ -n "$POOL_ID" ]; then
    echo "  ✓ User pool already exists: $POOL_ID"
else
    POOL_ID=$(aws cognito-idp create-user-pool \
        --pool-name "$POOL_NAME" \
        --policies "PasswordPolicy={MinimumLength=8,RequireUppercase=false,RequireNumbers=false,RequireSymbols=false}" \
        --auto-verified-attributes email \
        --username-attributes email \
        --region "$REGION" \
        --query UserPool.Id --output text)
    echo "  ✓ User pool created: $POOL_ID"
fi

# App client
CLIENT_ID=$(aws cognito-idp list-user-pool-clients --user-pool-id "$POOL_ID" --region "$REGION" \
    --query "UserPoolClients[?ClientName=='$CLIENT_NAME'].ClientId" --output text)

if [ -n "$CLIENT_ID" ]; then
    echo "  ✓ App client already exists: $CLIENT_ID"
else
    CLIENT_ID=$(aws cognito-idp create-user-pool-client \
        --user-pool-id "$POOL_ID" \
        --client-name "$CLIENT_NAME" \
        --no-generate-secret \
        --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
        --region "$REGION" \
        --query UserPoolClient.ClientId --output text)
    echo "  ✓ App client created: $CLIENT_ID"
fi

# Add Cognito authorizer to API Gateway
AUTH_ID=$(aws apigatewayv2 get-authorizers --api-id "$API_ID" --region "$REGION" \
    --query "Items[?Name=='cognito'].AuthorizerId" --output text)

if [ -n "$AUTH_ID" ] && [ "$AUTH_ID" != "None" ]; then
    echo "  ✓ Cognito authorizer already exists"
else
    POOL_ARN="arn:aws:cognito-idp:${REGION}:${ACCOUNT_ID}:userpool/${POOL_ID}"
    AUTH_ID=$(aws apigatewayv2 create-authorizer \
        --api-id "$API_ID" \
        --authorizer-type JWT \
        --identity-source '$request.header.Authorization' \
        --name cognito \
        --jwt-configuration "Audience=$CLIENT_ID,Issuer=https://cognito-idp.${REGION}.amazonaws.com/${POOL_ID}" \
        --region "$REGION" \
        --query AuthorizerId --output text)
    echo "  ✓ Cognito authorizer added"
fi

# Create routes with Cognito authorizer
for ROUTE in "GET /state" "POST /state"; do
    ROUTE_KEY="$ROUTE"
    aws apigatewayv2 create-route \
        --api-id "$API_ID" \
        --route-key "$ROUTE_KEY" \
        --target "integrations/$INTEGRATION_ID" \
        --authorization-type JWT \
        --authorizer-id "$AUTH_ID" \
        --region "$REGION" 2>/dev/null || true
done

# OPTIONS route (no auth)
aws apigatewayv2 create-route \
    --api-id "$API_ID" \
    --route-key "OPTIONS /{proxy+}" \
    --target "integrations/$INTEGRATION_ID" \
    --region "$REGION" 2>/dev/null || true

# Deploy API
STAGE_ID=$(aws apigatewayv2 get-stages --api-id "$API_ID" --region "$REGION" \
    --query "Items[?StageName=='\$default'].StageName" --output text)

if [ -z "$STAGE_ID" ]; then
    aws apigatewayv2 create-stage \
        --api-id "$API_ID" \
        --stage-name '$default' \
        --auto-deploy \
        --region "$REGION" > /dev/null
fi

# ── Step 6: Create Cognito users ───────────────────────────────────────────────
echo ""
echo "▶ Step 6: Creating Cognito users from cadence.yaml"

_USERS_FILE=$(mktemp)
python3 - <<'PYEOF' > "$_USERS_FILE"
import yaml, json
with open("cadence.yaml") as f:
    config = yaml.safe_load(f)
for user in config.get("users", []):
    print(json.dumps({"email": user.get("email", ""), "name": user.get("name", user.get("id", ""))}))
PYEOF

while IFS= read -r line; do
    EMAIL=$(echo "$line" | python3 -c "import json,sys; print(json.load(sys.stdin)['email'])")
    NAME=$(echo "$line" | python3 -c "import json,sys; print(json.load(sys.stdin)['name'])")

    if aws cognito-idp admin-get-user --user-pool-id "$POOL_ID" --username "$EMAIL" --region "$REGION" &>/dev/null; then
        echo "  ✓ User already exists: $NAME ($EMAIL)"
    else
        TEMP_PW=$(python3 -c "import secrets, string; print(secrets.token_urlsafe(12) + '!A1a')")
        aws cognito-idp admin-create-user \
            --user-pool-id "$POOL_ID" \
            --username "$EMAIL" \
            --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
            --temporary-password "$TEMP_PW" \
            --message-action SUPPRESS \
            --region "$REGION" > /dev/null
        echo "  ✓ Created user: $NAME ($EMAIL) — temp password: $TEMP_PW"
    fi
done < "$_USERS_FILE"
rm -f "$_USERS_FILE"

# ── Step 7: S3 bucket ─────────────────────────────────────────────────────────
echo ""
echo "▶ Step 7: Creating S3 bucket"

BUCKET=$(python3 -c "import yaml; c=yaml.safe_load(open('cadence.yaml')); print(c.get('aws',{}).get('s3_bucket',''))")

if [ -z "$BUCKET" ]; then
    BUCKET="cadence-${PROJECT_NAME}-${ACCOUNT_ID}"
    echo "  No s3_bucket in cadence.yaml — using: $BUCKET"
fi

if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    echo "  ✓ Bucket already exists: $BUCKET"
else
    if [ "$REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$BUCKET"
    else
        aws s3api create-bucket --bucket "$BUCKET" \
            --create-bucket-configuration "LocationConstraint=$REGION"
    fi
    # Block public access (CloudFront will serve it)
    aws s3api put-public-access-block --bucket "$BUCKET" \
        --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
    echo "  ✓ Bucket created: $BUCKET"
fi

# ── Save config values back ───────────────────────────────────────────────────
echo ""
echo "▶ Updating cadence.yaml with created resource IDs..."

python3 - "$POOL_ID" "$CLIENT_ID" "$API_URL" "$BUCKET" <<'PYEOF'
import yaml, sys
pool_id, client_id, api_url, bucket = sys.argv[1:]
with open("cadence.yaml") as f:
    config = yaml.safe_load(f)
config.setdefault("aws", {})
config["aws"]["cognito_user_pool_id"] = pool_id
config["aws"]["cognito_client_id"] = client_id
config["aws"]["api_url"] = api_url
config["aws"]["s3_bucket"] = bucket
with open("cadence.yaml", "w") as f:
    yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
print("  ✓ cadence.yaml updated")
PYEOF

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "================================="
echo "  ✅ AWS setup complete!"
echo ""
echo "  DynamoDB:    $TABLE_NAME"
echo "  Lambda:      $LAMBDA_NAME"
echo "  API URL:     $API_URL"
echo "  Cognito:     $POOL_ID"
echo "  S3 Bucket:   $BUCKET"
echo ""
echo "  Next steps:"
echo "  1. python scripts/build.py"
echo "  2. python scripts/deploy.py"
echo "  3. Set up CloudFront distribution pointing to s3://$BUCKET"
echo "     (or access via S3 website URL for testing)"
echo ""
echo "  Temp passwords were printed above — share them with each user."
echo "  Each user will be prompted to set a new password on first sign-in."
echo "================================="
