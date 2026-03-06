#!/bin/bash
# cadence — full setup
# Runs setup.py (CloudFormation) → deploy.py in sequence.
# Usage: bash scripts/setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."

cd "$ROOT"

echo "================================="
echo "  Cadence — Full Setup"
echo "================================="
echo ""
echo "This will run:"
echo "  1. setup.py  — CloudFormation stack (all AWS infrastructure)"
echo "  2. deploy.py — Build and deploy the dashboard"
echo ""
read -p "Continue? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
    echo "Aborted."
    exit 1
fi
echo ""

# Step 1: CloudFormation infrastructure
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Step 1/2: AWS Infrastructure (CloudFormation)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
python3 "$SCRIPT_DIR/setup.py"

# Step 2: Deploy
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Step 2/2: Build & Deploy"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
python3 scripts/deploy.py

# Done
CF_URL=$(python3 -c "import yaml; print(yaml.safe_load(open('cadence.yaml')).get('aws',{}).get('cloudfront_url',''))" 2>/dev/null || echo "")

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Cadence is live!"
echo ""
if [ -n "$CF_URL" ]; then
    echo "  Dashboard: $CF_URL"
else
    echo "  Dashboard URL is in cadence.yaml → aws.cloudfront_url"
fi
echo ""
echo "  Temp passwords were printed above — share them"
echo "  with each user for their first sign-in."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
