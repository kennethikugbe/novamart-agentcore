#!/usr/bin/env python3
"""
check_models.py — verify the three Bedrock models this project needs.

Run from the project root with the .venv active:

    python check_models.py

It makes one real API call per model and prints PASS / FAIL with the fix.
Nothing is created, nothing is deployed, nothing costs more than a fraction
of a cent.
"""

import sys

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

REGION = "us-east-1"

HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
TITAN_EMBED = "amazon.titan-embed-text-v2:0"

OK = "\033[92mPASS\033[0m"
BAD = "\033[91mFAIL\033[0m"


def _explain(exc):
    """Turn a boto3 error into an actionable sentence."""
    code = ""
    msg = str(exc)
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        msg = exc.response.get("Error", {}).get("Message", str(exc))

    if code in ("AccessDeniedException", "AccessDenied"):
        if "use case" in msg.lower() or "agreement" in msg.lower():
            return ("Anthropic use-case details not submitted yet.\n"
                    "  FIX: Bedrock console -> Playground -> select this Claude model.\n"
                    "       A form appears once; submit it, then re-run this script.")
        return ("IAM policy denies this model.\n"
                "  FIX: your account/role lacks bedrock:InvokeModel.\n"
                f"  Detail: {msg}")
    if code == "ResourceNotFoundException":
        return (f"Model or inference profile not found in {REGION}.\n"
                "  FIX: confirm the region is us-east-1 and the model ID is exact.")
    if code in ("ThrottlingException", "TooManyRequestsException"):
        return "Rate limited. Wait a few seconds and re-run."
    if code == "ValidationException":
        return (f"Request rejected: {msg}\n"
                "  FIX: if it mentions the model identifier, the inference\n"
                "       profile is wrong for this region.")
    return f"{code or type(exc).__name__}: {msg}"


def check_claude(runtime, label, model_id):
    print(f"[{label}] {model_id}")
    try:
        resp = runtime.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "Reply with exactly: OK"}]}],
            inferenceConfig={"maxTokens": 10, "temperature": 0},
        )
        text = resp["output"]["message"]["content"][0]["text"].strip()
        print(f"  {OK}  model replied: {text!r}")
        return True
    except (ClientError, NoCredentialsError) as exc:
        print(f"  {BAD}  {_explain(exc)}")
        return False


def check_titan(runtime):
    print(f"[Titan Embeddings V2] {TITAN_EMBED}")
    try:
        import json
        resp = runtime.invoke_model(
            modelId=TITAN_EMBED,
            body=json.dumps({"inputText": "NovaMart return policy"}),
        )
        payload = json.loads(resp["body"].read())
        dims = len(payload.get("embedding", []))
        print(f"  {OK}  returned a {dims}-dimension vector")
        return True
    except (ClientError, NoCredentialsError) as exc:
        print(f"  {BAD}  {_explain(exc)}")
        return False


def main():
    print("=" * 62)
    print("  Bedrock model check  (region: us-east-1)")
    print("=" * 62)

    try:
        ident = boto3.client("sts", region_name=REGION).get_caller_identity()
        print(f"  Account: {ident['Account']}")
    except Exception as exc:
        print(f"  {BAD}  cannot reach AWS: {_explain(exc)}")
        print("  Refresh your credentials (see AWS_CREDENTIALS_RUNBOOK.md).")
        return 1

    runtime = boto3.client("bedrock-runtime", region_name=REGION)

    results = [
        check_claude(runtime, "Claude Haiku 4.5  ", HAIKU),
        check_claude(runtime, "Claude Sonnet 4.5 ", SONNET),
        check_titan(runtime),
    ]

    print("-" * 62)
    if all(results):
        print(f"  {OK}  All three models usable. Ready for Knowledge Bases.")
        return 0

    print(f"  {BAD}  Some models are not usable yet - fix the FAILs above.")
    print("  You CAN still create Knowledge Bases, but check again before deploying.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
