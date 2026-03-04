#!/bin/bash
# cadence — prerequisite checks
# Sourced by setup scripts. Validates that everything is in place before touching AWS.
# Usage: source scripts/preflight.sh

_fail=0

_check() {
    local label="$1"
    local ok="$2"
    local hint="$3"
    if [ "$ok" = "1" ]; then
        printf "  ✓ %-28s\n" "$label"
    else
        printf "  ✗ %-28s %s\n" "$label" "$hint"
        _fail=1
    fi
}

echo "▶ Preflight checks"
echo ""

# 1. AWS CLI installed
if command -v aws &>/dev/null; then
    _aws_ver=$(aws --version 2>&1 | head -1)
    _check "AWS CLI" "1"
else
    _check "AWS CLI" "0" "Install: https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html"
fi

# 2. AWS credentials valid
if aws sts get-caller-identity &>/dev/null; then
    _account=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
    _check "AWS credentials (${_account})" "1"
else
    _check "AWS credentials" "0" "Run 'aws configure' or 'aws sso login'"
fi

# 3. Python 3
if command -v python3 &>/dev/null; then
    _py_ver=$(python3 --version 2>&1)
    _check "Python 3 (${_py_ver})" "1"
else
    _check "Python 3" "0" "Install Python 3.9+: https://www.python.org/downloads/"
fi

# 4. pyyaml
if python3 -c "import yaml" &>/dev/null; then
    _check "pyyaml" "1"
else
    _check "pyyaml" "0" "Run: pip install pyyaml"
fi

# 5. boto3
if python3 -c "import boto3" &>/dev/null; then
    _check "boto3" "1"
else
    _check "boto3" "0" "Run: pip install boto3"
fi

# 6. cadence.yaml exists
if [ -f "cadence.yaml" ]; then
    _check "cadence.yaml" "1"
else
    _check "cadence.yaml" "0" "Run: cp cadence.example.yaml cadence.yaml"
fi

# 7. items.csv exists (read csv path from config)
if [ -f "cadence.yaml" ]; then
    _csv=$(python3 -c "import yaml; print(yaml.safe_load(open('cadence.yaml')).get('csv','items.csv'))" 2>/dev/null || echo "items.csv")
    if [ -f "$_csv" ]; then
        _check "CSV file ($_csv)" "1"
    else
        _check "CSV file ($_csv)" "0" "Create your items CSV or cp items.example.csv items.csv"
    fi
fi

# 8. Region is set
if [ -f "cadence.yaml" ]; then
    _region=$(python3 -c "import yaml; print(yaml.safe_load(open('cadence.yaml')).get('aws',{}).get('region',''))" 2>/dev/null || echo "")
    if [ -n "$_region" ]; then
        _check "AWS region ($_region)" "1"
    else
        _check "AWS region" "0" "Set aws.region in cadence.yaml"
    fi
fi

# 9. At least one user defined
if [ -f "cadence.yaml" ]; then
    _user_count=$(python3 -c "import yaml; print(len(yaml.safe_load(open('cadence.yaml')).get('users',[])))" 2>/dev/null || echo "0")
    if [ "$_user_count" -gt 0 ]; then
        _check "Users defined ($_user_count)" "1"
    else
        _check "Users defined" "0" "Add at least one user to cadence.yaml"
    fi
fi

echo ""

if [ "$_fail" = "1" ]; then
    echo "  ✗ Preflight failed — fix the issues above and try again."
    exit 1
else
    echo "  ✓ All checks passed"
    echo ""
fi
