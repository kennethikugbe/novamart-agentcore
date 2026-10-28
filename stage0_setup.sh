#!/usr/bin/env bash
# ============================================================
#  NovaMart AgentCore — Stage 0 helper
#  Deploys the foundation stack, seeds data, verifies config.
#
#  Usage (Git Bash, from the project root ~/novamart):
#      bash stage0_setup.sh
#
#  Safe to re-run. The CloudFormation step is idempotent:
#  an unchanged stack reports "No updates are to be performed".
# ============================================================

set -u

STACK_NAME="udacity-agentcore"
REGION="us-east-1"
TEMPLATE="infrastructure/starter_stack.yaml"

step() { echo; echo "=================================================="; echo "  $1"; echo "=================================================="; }

# ---------- 0. Sanity checks ----------
step "0. Environment check"

if [ ! -f "$TEMPLATE" ]; then
  echo "ERROR: $TEMPLATE not found."
  echo "Run this script from the project root (the folder containing config.py)."
  exit 1
fi
echo "OK  template found: $TEMPLATE"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
if [ -z "${ACCOUNT:-}" ]; then
  echo "ERROR: AWS credentials are not working."
  echo "Run: aws sts get-caller-identity"
  echo "If it says ExpiredToken, refresh credentials (see AWS_CREDENTIALS_RUNBOOK.md)."
  exit 1
fi
echo "OK  account: $ACCOUNT"

CURRENT_REGION=$(aws configure get region 2>/dev/null)
if [ "$CURRENT_REGION" != "$REGION" ]; then
  echo "WARN region was '$CURRENT_REGION' - setting it to $REGION"
  aws configure set region "$REGION"
fi
echo "OK  region: $REGION"

# ---------- 1. Deploy the stack ----------
step "1. Deploying CloudFormation stack '$STACK_NAME' (this takes 8-20 minutes)"
echo "Progress is silent. Do NOT press Ctrl+C."
echo

aws cloudformation deploy \
  --template-file "$TEMPLATE" \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION"

DEPLOY_RC=$?

STATUS=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].StackStatus" \
  --output text 2>/dev/null)

echo
echo "Stack status: ${STATUS:-UNKNOWN}"

if [ "$STATUS" != "CREATE_COMPLETE" ] && [ "$STATUS" != "UPDATE_COMPLETE" ]; then
  echo
  echo "DEPLOY DID NOT SUCCEED (exit code $DEPLOY_RC)."
  echo "First failures:"
  aws cloudformation describe-stack-events \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "StackEvents[?ResourceStatus=='CREATE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
    --output text 2>/dev/null | head -20
  echo
  echo "If the stack is ROLLBACK_COMPLETE it cannot be updated. Delete then retry:"
  echo "  aws cloudformation delete-stack --stack-name $STACK_NAME --region $REGION"
  exit 1
fi

# ---------- 2. Seed data ----------
step "2. Seeding DynamoDB tables and S3 policy documents"
if python infrastructure/seed_data.py; then
  echo "OK  seed_data.py completed"
else
  echo "WARN seed_data.py failed - see the error above"
fi

# ---------- 3. Verify data ----------
step "3. Verifying seed data"

echo "-- orders count --"
aws dynamodb scan --table-name "${STACK_NAME}-orders" --select COUNT \
  --region "$REGION" --query "Count" --output text 2>/dev/null

echo "-- customers count --"
aws dynamodb scan --table-name "${STACK_NAME}-customers" --select COUNT \
  --region "$REGION" --query "Count" --output text 2>/dev/null

echo "-- policy documents in S3 --"
if [ ! -f .env ]; then cp .env.example .env; echo "(created .env from .env.example)"; fi
POLICY_BUCKET=$(python -c "import config; print(config.POLICY_BUCKET)" 2>/dev/null)
if [ -n "${POLICY_BUCKET:-}" ]; then
  aws s3 ls "s3://${POLICY_BUCKET}/policies/" --recursive 2>/dev/null | head -30
else
  echo "WARN could not resolve POLICY_BUCKET from config.py"
fi

# ---------- 4. Config table ----------
step "4. python config.py"
python config.py

echo
echo "=================================================="
echo "  Done. Expected: KB IDs and Runtime ARN still"
echo "  show (not yet created). That is correct."
echo "=================================================="
