#!/usr/bin/env python3
"""
preflight_deploy.py — go / no-go check before `python src/agent_orchestrator.py deploy`

The deploy runs `agentcore deploy -y`, which packages the runtime with uv and
creates/updates it through AWS CDK. It needs Node.js 20+, the AgentCore CLI,
uv, live AWS credentials, and all three Knowledge Base IDs.

    python preflight_deploy.py

Exit code 0 = safe to deploy.
"""

import json
import os
import re
import shutil
import subprocess
import sys

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[1m", "\033[0m")

failures = []
warnings = []


def ok(msg):
    print(f"  {GREEN}PASS{RESET}  {msg}")


def bad(msg, fix):
    print(f"  {RED}FAIL{RESET}  {msg}")
    print(f"        {DIM}fix: {fix}{RESET}")
    failures.append(msg)


def warn(msg, note=""):
    print(f"  {YELLOW}WARN{RESET}  {msg}")
    if note:
        print(f"        {DIM}{note}{RESET}")
    warnings.append(msg)


def head(msg):
    print()
    print("=" * 72)
    print(f"  {msg}")
    print("=" * 72)


def run(cmd):
    """Return (rc, stdout+stderr first line)."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        out = (p.stdout or p.stderr or "").strip().splitlines()
        return p.returncode, (out[0] if out else "")
    except Exception as exc:
        return 1, str(exc)


def main():
    head("1. Python environment")
    v = sys.version_info
    if v.major == 3 and v.minor == 12:
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        bad(f"Python {v.major}.{v.minor}.{v.micro} (need 3.12)",
            "the runtime is PYTHON_3_12; recreate the venv with py -3.12 -m venv .venv")

    for mod in ("boto3", "strands", "dotenv"):
        try:
            __import__(mod)
            ok(f"module '{mod}' importable")
        except ImportError:
            bad(f"module '{mod}' missing", "pip install -r requirements.txt")

    try:
        import boto3
        if tuple(int(x) for x in boto3.__version__.split(".")[:2]) >= (1, 42):
            ok(f"boto3 {boto3.__version__}")
        else:
            bad(f"boto3 {boto3.__version__} too old",
                "pip install -U boto3 botocore  (guardrail needs >= 1.42)")
    except Exception:
        pass

    head("2. Deploy tooling (AgentCore CLI stack)")
    rc, out = run("node --version")
    if rc == 0:
        major = int(re.sub(r"[^0-9]", "", out.split(".")[0]) or 0)
        if major >= 20:
            ok(f"Node.js {out}")
        else:
            bad(f"Node.js {out} - need 20+", "install Node 20 LTS and reopen the terminal")
    else:
        bad("node not found", "install Node.js 20+ (https://nodejs.org)")

    rc, out = run("npm --version")
    ok(f"npm {out}") if rc == 0 else bad("npm not found", "comes with Node.js")

    agentcore = shutil.which("agentcore")
    if agentcore:
        rc, out = run("agentcore --version")
        if "configure" in out.lower() or rc != 0:
            bad("the 'agentcore' on PATH looks like the old Python toolkit",
                "pip uninstall bedrock-agentcore-starter-toolkit, then "
                "npm install -g @aws/agentcore@0.30.0")
        else:
            ok(f"agentcore CLI {out}  ({agentcore})")
    else:
        bad("agentcore CLI not on PATH",
            "npm install -g @aws/agentcore@0.30.0   then reopen the terminal")

    rc, out = run("uv --version")
    if rc == 0:
        ok(f"uv {out}")
    else:
        bad("uv not found", "curl -LsSf https://astral.sh/uv/install.sh | sh  (or pip install uv)")

    head("3. Project files")
    for path in ("src/agent_orchestrator.py", "agentcore/agentcore.json",
                 "infrastructure/starter_stack.yaml", ".env"):
        if os.path.exists(path):
            ok(f"{path} exists")
        else:
            bad(f"{path} missing", "run from the project root; .env comes from .env.example")

    try:
        src = open("src/agent_orchestrator.py").read()
        todos = src.count("TODO")
        if todos == 0:
            ok("src/agent_orchestrator.py has no TODO stubs left")
        else:
            bad(f"{todos} TODO stub(s) still in src/agent_orchestrator.py",
                "finish Tasks 2, 3, 4 and 6 before deploying")
        for fn in ("create_guardrail", "deploy_to_agentcore_runtime",
                   "configure_memory", "configure_observability"):
            if f"def {fn}" in src:
                ok(f"{fn}() present")
            else:
                bad(f"{fn}() missing", "restore the complete agent_orchestrator.py")
        if "raise NotImplementedError" in src:
            bad("file still contains 'raise NotImplementedError'",
                "an unimplemented stub is still in place")
    except FileNotFoundError:
        pass

    head("4. .env values")
    env = {}
    if os.path.exists(".env"):
        for line in open(".env"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, val = line.partition("=")
                env[k.strip()] = val.strip()

    for key in ("RETURNS_KB_ID", "SHIPPING_KB_ID", "WARRANTY_KB_ID"):
        if env.get(key):
            ok(f"{key} = {env[key]}")
        else:
            bad(f"{key} empty", "run create_knowledge_bases.py, or paste the ID from the console")

    if env.get("GUARDRAIL_ID"):
        warn(f"GUARDRAIL_ID already set ({env['GUARDRAIL_ID']})",
             "deploy will create/reuse the guardrail and print the values again")
    else:
        ok("GUARDRAIL_ID empty (deploy will create it)")

    head("5. AWS identity and region")
    try:
        import boto3
        region = boto3.session.Session().region_name or os.environ.get("AWS_DEFAULT_REGION")
        if region == "us-east-1":
            ok(f"region {region}")
        else:
            bad(f"region is {region}, need us-east-1",
                "aws configure set region us-east-1")

        sts = boto3.client("sts", region_name="us-east-1")
        ident = sts.get_caller_identity()
        ok(f"account {ident['Account']}")
        ok(f"identity {ident['Arn'][:60]}...")

        cf = boto3.client("cloudformation", region_name="us-east-1")
        status = cf.describe_stacks(StackName="udacity-agentcore")["Stacks"][0]["StackStatus"]
        ok(f"foundation stack {status}") if status in (
            "CREATE_COMPLETE", "UPDATE_COMPLETE") else bad(
            f"foundation stack is {status}", "redeploy infrastructure/starter_stack.yaml")

        agent = boto3.client("bedrock-agent", region_name="us-east-1")
        names = {kb["name"]: kb["knowledgeBaseId"]
                 for kb in agent.list_knowledge_bases().get("knowledgeBaseSummaries", [])}
        for want in ("novamart-returns-policy-kb", "novamart-shipping-policy-kb",
                     "novamart-warranty-policy-kb"):
            if want in names:
                ok(f"{want} -> {names[want]}")
            else:
                warn(f"{want} not found by name", "check the KB IDs in .env match a real KB")

    except Exception as exc:
        bad(f"AWS check failed: {type(exc).__name__}: {exc}",
            "refresh credentials - see AWS_CREDENTIALS_RUNBOOK.md")

    print()
    print("=" * 72)
    if not failures:
        print(f"  {GREEN}{BOLD}GO - safe to deploy.{RESET}")
        if warnings:
            print(f"  {YELLOW}{len(warnings)} warning(s) above - read them, they are not fatal.{RESET}")
        print()
        print("  Keep the terminal awake: the deploy takes 5-10 minutes and needs")
        print("  live credentials the whole time.")
        print()
        print(f"  {BOLD}python src/agent_orchestrator.py deploy{RESET}")
    else:
        print(f"  {RED}{BOLD}NO-GO - {len(failures)} blocker(s).{RESET}")
        print("  Fix the FAIL lines above, then re-run: python preflight_deploy.py")
    print("=" * 72)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
