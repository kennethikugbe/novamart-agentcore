#!/usr/bin/env bash
# ============================================================
#  push_to_github.sh — safely publish the project for submission
#
#  Usage (Git Bash, from the project root ~/novamart):
#
#      bash push_to_github.sh https://github.com/<you>/<repo>.git
#
#  What it does:
#    1. verifies .env is populated (it is a graded deliverable)
#    2. scans tracked files for leaked AWS credentials and ABORTS if found
#    3. makes .env committable (the starter's .gitignore excludes it)
#    4. initialises git, commits, sets the remote and pushes
#
#  Safe to re-run: subsequent runs create new commits and push again.
# ============================================================

set -u

REMOTE_URL="${1:-}"
GREEN=$'\033[92m'; RED=$'\033[91m'; YEL=$'\033[93m'; DIM=$'\033[2m'; OFF=$'\033[0m'

step() { echo; echo "=================================================="; echo "  $1"; echo "=================================================="; }
ok()   { echo "  ${GREEN}OK${OFF}    $1"; }
warn() { echo "  ${YEL}WARN${OFF}  $1"; }
die()  { echo "  ${RED}STOP${OFF}  $1"; exit 1; }

if [ -z "$REMOTE_URL" ]; then
  echo "Usage: bash push_to_github.sh https://github.com/<you>/<repo>.git"
  exit 1
fi

[ -f config.py ] || die "run this from the project root (the folder containing config.py)"

# ------------------------------------------------------------
step "1. Check .env is complete"
# ------------------------------------------------------------
[ -f .env ] || die ".env is missing - it is a graded deliverable"

MISSING=""
for key in RETURNS_KB_ID SHIPPING_KB_ID WARRANTY_KB_ID AGENTCORE_RUNTIME_ARN GUARDRAIL_ID GUARDRAIL_VERSION; do
  if ! grep -qE "^${key}=.+" .env; then MISSING="$MISSING $key"; fi
done

if [ -n "$MISSING" ]; then
  die ".env is missing values for:$MISSING
       Fill them in before pushing - the rubric requires a populated .env"
fi
ok "all six required values present in .env"

# ------------------------------------------------------------
step "2. Stage files and scan ONLY what will be committed"
# ------------------------------------------------------------
if [ -d .git ]; then
  ok "git repository already initialised"
else
  git init -q
  git branch -M main 2>/dev/null || true
  ok "initialised git repository"
fi

# .env is a graded deliverable; the starter .gitignore excludes it, so allow
# it explicitly (it holds resource IDs only - AWS keys live in ~/.aws/credentials)
if ! grep -q '^!.env$' .gitignore 2>/dev/null; then
  {
    echo ""
    echo "# ------------------------------------------------------------"
    echo "# Submission: .env is a required deliverable (resource IDs only,"
    echo "# no secrets - AWS keys live in ~/.aws/credentials)"
    echo "# ------------------------------------------------------------"
    echo "!.env"
    echo ".account-id"
    echo ".env.from-previous-account"
    echo ".env.old"
  } >> .gitignore
  ok "added '!.env' exception to .gitignore"
else
  ok ".gitignore already allows .env"
fi

git add -A || die "git add failed"

# Only these files are going to GitHub - so only these need scanning.
STAGED=$(git diff --cached --name-only)
if [ -z "$STAGED" ]; then
  warn "nothing staged (working tree clean) - skipping scan"
else
  SCAN_HITS=0
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    case "$f" in
      push_to_github.sh) continue ;;                 # contains the pattern itself
      *.png|*.jpg|*.jpeg|*.zip|*.pyc) continue ;;    # binary
    esac
    # AKIA/ASIA access key IDs and IQoJ session tokens, excluding AWS's own
    # placeholder examples (they always contain the literal EXAMPLE).
    HITS=$(grep -nE '(AKIA|ASIA)[A-Z0-9]{16}|IQoJb3JpZ2luX2Vj' "$f" 2>/dev/null \
             | grep -v 'EXAMPLE' \
             | grep -v 'xxxxxxxx' || true)
    if [ -n "$HITS" ]; then
      echo "  ${RED}LEAK${OFF}  $f"
      echo "$HITS" | head -3 | sed 's/^/          /'
      SCAN_HITS=$((SCAN_HITS + 1))
    fi
  done <<< "$STAGED"

  if [ "$SCAN_HITS" -gt 0 ]; then
    echo
    die "$SCAN_HITS file(s) above contain what looks like real AWS credentials.
       Replace the value with <REDACTED>, 'git add' again, and re-run."
  fi
  ok "no real AWS credentials in the $(echo "$STAGED" | wc -l | tr -d ' ') staged file(s)"
fi

# .env must actually be staged - it is a graded deliverable
if echo "$STAGED" | grep -qx '.env'; then
  ok ".env is staged for commit"
else
  die ".env is NOT staged. Check .gitignore contains the line: !.env"
fi

# ------------------------------------------------------------
step "3. Commit"
# ------------------------------------------------------------
if git diff --cached --quiet; then
  warn "nothing new to commit"
else
  git -c user.name="${GIT_AUTHOR_NAME:-$(git config user.name || echo 'NovaMart Student')}" \
      -c user.email="${GIT_AUTHOR_EMAIL:-$(git config user.email || echo 'student@example.com')}" \
      commit -q -m "NovaMart multi-agent customer support on Amazon Bedrock AgentCore

Tasks 2, 3, 4 and 6 implemented in src/agent_orchestrator.py:
  - 5 Strands agents (Orchestrator -> Inventory/Policy/Refund/Communication)
  - parallel multi-agent RAG across 3 Bedrock Knowledge Bases (ThreadPoolExecutor)
  - shared DynamoDB WorkflowState with optimistic locking
  - Bedrock Guardrail (content/PII/topic/word policies, numbered version)
  - AgentCore Runtime deployment via the AgentCore CLI
  - AgentCore Memory (SESSION_SUMMARY, 7-day expiry)
  - CloudWatch Logs + X-Ray observability (100% sampling)

Task 5: three synced S3 Vectors Knowledge Bases.
Evidence screenshots in evidence/."
  ok "commit created"
fi

# ------------------------------------------------------------
step "4. Push"
# ------------------------------------------------------------
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE_URL"
else
  git remote add origin "$REMOTE_URL"
fi
ok "remote origin = $REMOTE_URL"

echo
echo "  Pushing... (Git Credential Manager may open a browser to sign in)"
echo
if git push -u origin main; then
  ok "pushed to main"
else
  echo
  echo "  If the remote already has commits, try:"
  echo "    git pull --rebase origin main"
  echo "    git push -u origin main"
  exit 1
fi

# ------------------------------------------------------------
echo
echo "=================================================="
echo "  Done. Your submission URL is:"
echo "    ${REMOTE_URL%.git}"
echo "=================================================="
echo
echo "  Final checks before submitting:"
echo "    1. Open the repo in a private/incognito window (as a reviewer would)"
echo "    2. Confirm src/agent_orchestrator.py has no TODO markers"
echo "    3. Confirm .env is visible and populated"
echo "    4. Confirm evidence/ screenshots render"
echo
