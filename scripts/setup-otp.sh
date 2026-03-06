#!/bin/bash
# cadence — enable passwordless email OTP login
# Run after setup-aws.sh. Adds CUSTOM_AUTH flow to existing Cognito setup.
# Prerequisites: setup-aws.sh completed, SES sender email configured.
# Usage: bash scripts/setup-otp.sh

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
print(f'POOL_ID="{aws.get("cognito_user_pool_id", "")}"')
print(f'CLIENT_ID="{aws.get("cognito_client_id", "")}"')
print(f'SES_SENDER="{c.get("ses_sender_email", "")}"')
PYEOF
source "$_CADENCE_ENV"
rm -f "$_CADENCE_ENV"

if [ -z "$POOL_ID" ] || [ -z "$CLIENT_ID" ]; then
    echo "Error: Cognito not set up yet. Run scripts/setup-aws.sh first."
    exit 1
fi

if [ -z "$SES_SENDER" ]; then
    echo "Error: aws.ses_sender_email not set in cadence.yaml."
    echo "  Add it to cadence.yaml under aws:, e.g.:"
    echo "    aws:"
    echo "      ses_sender_email: noreply@yourdomain.com"
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
LAMBDA_ROLE="cadence-lambda-role"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE}"
TRIGGER_LAMBDA="cadence-auth-triggers"

echo "================================="
echo "  Cadence OTP Setup"
echo "  Region:  $REGION"
echo "  Pool:    $POOL_ID"
echo "  Sender:  $SES_SENDER"
echo "================================="
echo ""

# ── Step 1: Verify SES sender email ─────────────────────────────────────────
echo "▶ Step 1: Verifying SES sender email"

SES_STATUS=$(aws ses get-identity-verification-attributes \
    --identities "$SES_SENDER" \
    --region "$REGION" \
    --query "VerificationAttributes.\"$SES_SENDER\".VerificationStatus" \
    --output text 2>/dev/null || echo "None")

if [ "$SES_STATUS" = "Success" ]; then
    echo "  ✓ $SES_SENDER already verified"
else
    aws ses verify-email-identity --email-address "$SES_SENDER" --region "$REGION"
    echo "  ✓ Verification email sent to $SES_SENDER"
    echo "    ⚠ Check your inbox and click the verification link before using OTP."
    echo ""
    echo "  Press Enter once you've verified the email (or Ctrl+C to quit)..."
    read -r
fi

# ── Step 2: Check SES sandbox & verify user emails if needed ─────────────────
echo ""
echo "▶ Step 2: Checking SES sending mode"

SES_SEND_QUOTA=$(aws ses get-send-quota --region "$REGION" --query "Max24HourSend" --output text 2>/dev/null || echo "0")

if (( $(echo "$SES_SEND_QUOTA <= 200" | bc -l 2>/dev/null || echo 1) )); then
    echo "  ⚠ SES is in sandbox mode (max 200 emails/day)."
    echo "    In sandbox mode, recipient emails must also be verified."
    echo "    Verifying user emails..."

    _USERS_FILE=$(mktemp)
    python3 - <<'PYEOF' > "$_USERS_FILE"
import yaml
with open("cadence.yaml") as f:
    config = yaml.safe_load(f)
for user in config.get("users", []):
    print(user.get("email", ""))
PYEOF

    while IFS= read -r EMAIL; do
        [ -z "$EMAIL" ] && continue
        STATUS=$(aws ses get-identity-verification-attributes \
            --identities "$EMAIL" \
            --region "$REGION" \
            --query "VerificationAttributes.\"$EMAIL\".VerificationStatus" \
            --output text 2>/dev/null || echo "None")
        if [ "$STATUS" = "Success" ]; then
            echo "  ✓ $EMAIL already verified"
        else
            aws ses verify-email-identity --email-address "$EMAIL" --region "$REGION"
            echo "  → Verification email sent to $EMAIL"
        fi
    done < "$_USERS_FILE"
    rm -f "$_USERS_FILE"

    echo ""
    echo "  Each user must click the SES verification link in their inbox."
    echo "  To skip sandbox mode, request production access in the SES console."
else
    echo "  ✓ SES is in production mode — no recipient verification needed"
fi

# ── Step 3: Add SES permission to Lambda role ────────────────────────────────
echo ""
echo "▶ Step 3: Adding SES send permission to Lambda role"

SES_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "ses:SendEmail",
    "Resource": "*"
  }]
}'

POLICY_NAME="cadence-ses-send"
if aws iam get-role-policy --role-name "$LAMBDA_ROLE" --policy-name "$POLICY_NAME" &>/dev/null; then
    echo "  ✓ SES policy already attached"
else
    aws iam put-role-policy \
        --role-name "$LAMBDA_ROLE" \
        --policy-name "$POLICY_NAME" \
        --policy-document "$SES_POLICY"
    echo "  ✓ SES send policy attached to $LAMBDA_ROLE"
fi

# ── Step 4: Deploy auth triggers Lambda ──────────────────────────────────────
echo ""
echo "▶ Step 4: Deploying auth triggers Lambda: $TRIGGER_LAMBDA"

cd "$ROOT/backend"
zip -q auth_triggers.zip auth_triggers.py
cd "$ROOT"

if aws lambda get-function --function-name "$TRIGGER_LAMBDA" --region "$REGION" &>/dev/null; then
    aws lambda update-function-code \
        --function-name "$TRIGGER_LAMBDA" \
        --zip-file fileb://backend/auth_triggers.zip \
        --region "$REGION" > /dev/null
    aws lambda wait function-updated-v2 --function-name "$TRIGGER_LAMBDA" --region "$REGION"
    aws lambda update-function-configuration \
        --function-name "$TRIGGER_LAMBDA" \
        --environment "Variables={SES_SENDER=$SES_SENDER}" \
        --region "$REGION" > /dev/null
    echo "  ✓ Lambda updated"
else
    aws lambda create-function \
        --function-name "$TRIGGER_LAMBDA" \
        --runtime python3.12 \
        --handler auth_triggers.handler \
        --role "$ROLE_ARN" \
        --zip-file fileb://backend/auth_triggers.zip \
        --environment "Variables={SES_SENDER=$SES_SENDER}" \
        --timeout 15 \
        --region "$REGION" > /dev/null
    echo "  ✓ Lambda created"
    echo "  Waiting for Lambda to become active..."
    aws lambda wait function-active-v2 --function-name "$TRIGGER_LAMBDA" --region "$REGION"
fi

TRIGGER_ARN=$(aws lambda get-function --function-name "$TRIGGER_LAMBDA" --region "$REGION" \
    --query 'Configuration.FunctionArn' --output text)

rm -f backend/auth_triggers.zip

# ── Step 5: Grant Cognito permission to invoke Lambda ────────────────────────
echo ""
echo "▶ Step 5: Adding Cognito invoke permission"

aws lambda add-permission \
    --function-name "$TRIGGER_LAMBDA" \
    --statement-id cognito-invoke \
    --action lambda:InvokeFunction \
    --principal cognito-idp.amazonaws.com \
    --source-arn "arn:aws:cognito-idp:${REGION}:${ACCOUNT_ID}:userpool/${POOL_ID}" \
    --region "$REGION" 2>/dev/null || echo "  ✓ Permission already exists"

echo "  ✓ Cognito can invoke $TRIGGER_LAMBDA"

# ── Step 6: Attach triggers to Cognito User Pool ────────────────────────────
echo ""
echo "▶ Step 6: Attaching auth triggers to Cognito User Pool"

aws cognito-idp update-user-pool \
    --user-pool-id "$POOL_ID" \
    --lambda-config \
        "DefineAuthChallenge=$TRIGGER_ARN,CreateAuthChallenge=$TRIGGER_ARN,VerifyAuthChallengeResponse=$TRIGGER_ARN" \
    --region "$REGION"

echo "  ✓ Triggers attached"

# ── Step 7: Add CUSTOM_AUTH to app client ────────────────────────────────────
echo ""
echo "▶ Step 7: Enabling CUSTOM_AUTH on app client"

aws cognito-idp update-user-pool-client \
    --user-pool-id "$POOL_ID" \
    --client-id "$CLIENT_ID" \
    --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_CUSTOM_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --region "$REGION" > /dev/null

echo "  ✓ CUSTOM_AUTH enabled"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "================================="
echo "  ✅ OTP setup complete!"
echo ""
echo "  Auth triggers: $TRIGGER_LAMBDA"
echo "  SES sender:    $SES_SENDER"
echo ""
echo "  Next: redeploy the frontend to enable the OTP login option:"
echo "    python3 scripts/deploy.py"
echo "================================="
