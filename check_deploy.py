#!/usr/bin/env python3
"""
check_deploy.py — read back what Stage 4 actually created, and tell you
exactly which rubric bullets are satisfied.

    python check_deploy.py

Checks the guardrail (all five policy families + numbered version), the
AgentCore Runtime (READY, PUBLIC, HTTP, the 8 env vars), AgentCore Memory
(strategy + 7-day expiry), the CloudWatch log group, X-Ray Transaction Search,
and whether traces have started arriving.
"""

import sys
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[1m", "\033[0m")

problems = []


def ok(m):
    print(f"  {GREEN}PASS{RESET}  {m}")


def bad(m, fix=""):
    print(f"  {RED}FAIL{RESET}  {m}")
    if fix:
        print(f"        {DIM}{fix}{RESET}")
    problems.append(m)


def warn(m, note=""):
    print(f"  {YELLOW}WARN{RESET}  {m}")
    if note:
        print(f"        {DIM}{note}{RESET}")


def head(m):
    print()
    print("=" * 74)
    print(f"  {m}")
    print("=" * 74)


def _safe_config(name, default=""):
    """Read a config attribute without letting a ConfigError crash the report."""
    try:
        import config
        return getattr(config, name, default) or default
    except Exception:
        return default


def session_health():
    """Fail fast with a clear diagnosis if the lab session has been revoked."""
    head("0. Lab session / credentials")
    try:
        sts = boto3.client("sts", region_name=REGION)
        ident = sts.get_caller_identity()
        ok(f"account {ident['Account']}")
        ok(f"identity {ident['Arn'][:70]}")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        msg = exc.response.get("Error", {}).get("Message", "")
        if "cancel-cred" in msg or "explicit deny" in msg:
            print(f"  {RED}{BOLD}LAB SESSION TERMINATED{RESET}")
            print(f"  {DIM}Vocareum attached the deny-all policy 'voc-cancel-cred'.{RESET}")
            print("  Every AWS call will fail until you start a new lab session.")
            print()
            print("  1. Udacity classroom -> Start Workspace")
            print("  2. In the workspace terminal:  cat ~/.aws/credentials")
            print("  3. Copy the 3 values into your laptop's ~/.aws/credentials")
            print("  4. aws sts get-caller-identity  -- check the ACCOUNT NUMBER")
            print("  5. python check_deploy.py")
            return False
        print(f"  {RED}FAIL{RESET}  {code}: {str(msg)[:160]}")
        print("  Refresh credentials - see AWS_CREDENTIALS_RUNBOOK.md")
        return False
    except Exception as exc:
        print(f"  {RED}FAIL{RESET}  {type(exc).__name__}: {exc}")
        return False

    try:
        boto3.client("cloudformation", region_name=REGION).list_exports()
        ok("CloudFormation ListExports permitted")
    except ClientError as exc:
        msg = exc.response.get("Error", {}).get("Message", "")
        if "cancel-cred" in msg:
            print(f"  {RED}{BOLD}LAB SESSION TERMINATED{RESET} - start a new workspace session")
            return False
        warn("CloudFormation ListExports denied (non-fatal here)")
    except Exception:
        pass
    return True


def main():
    env = {}
    for line in open(".env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()

    # ---------------- guardrail ----------------
    if not session_health():
        print()
        print("=" * 74)
        print(f"  {RED}{BOLD}Aborted: no usable AWS session.{RESET}")
        print("=" * 74)
        return 1

    head("1. Bedrock Guardrail")
    gr_id = env.get("GUARDRAIL_ID") or _safe_config("GUARDRAIL_ID")
    gr_ver = env.get("GUARDRAIL_VERSION") or _safe_config("GUARDRAIL_VERSION")

    if not gr_id:
        bad("GUARDRAIL_ID empty in .env", "copy it from the deploy output")
    else:
        ok(f"GUARDRAIL_ID = {gr_id}")
        if not gr_ver or gr_ver.upper() == "DRAFT":
            bad(f"GUARDRAIL_VERSION = {gr_ver or '(empty)'}",
                "must be a NUMBER (e.g. 1), never DRAFT")
        else:
            ok(f"GUARDRAIL_VERSION = {gr_ver} (numbered, not DRAFT)")

        bedrock = boto3.client("bedrock", region_name=REGION)
        try:
            # get_guardrail returns a FLAT response (no ['guardrail'] wrapper);
            # ask for the numbered version when we have it, else DRAFT.
            kwargs = {"guardrailIdentifier": gr_id}
            if gr_ver and gr_ver.upper() != "DRAFT":
                kwargs["guardrailVersion"] = str(gr_ver)
            _resp = bedrock.get_guardrail(**kwargs)
            g = _resp.get("guardrail") or _resp
            if "contentPolicy" not in g and "guardrailId" not in g:
                bad("get_guardrail returned an unexpected shape",
                    f"keys: {list(_resp.keys())[:10]}")
                raise StopIteration

            filters = {f["type"]: f.get("inputStrength")
                       for f in g.get("contentPolicy", {}).get("filters", [])}
            want = {"SEXUAL": "HIGH", "VIOLENCE": "HIGH", "HATE": "HIGH",
                    "INSULTS": "MEDIUM", "MISCONDUCT": "MEDIUM"}
            for t, strength in want.items():
                got = filters.get(t)
                if got == strength:
                    ok(f"content filter {t} = {got}")
                else:
                    bad(f"content filter {t} = {got or 'missing'}, expected {strength}")

            pii = {p["type"]: p.get("action")
                   for p in g.get("sensitiveInformationPolicy", {}).get("piiEntities", [])}
            for t, action in (("CREDIT_DEBIT_CARD_NUMBER", "BLOCK"),
                              ("US_SOCIAL_SECURITY_NUMBER", "BLOCK"),
                              ("EMAIL", "ANONYMIZE"),
                              ("PHONE", "ANONYMIZE")):
                got = pii.get(t)
                if got == action:
                    ok(f"PII {t} = {got}")
                else:
                    bad(f"PII {t} = {got or 'missing'}, expected {action}")

            topics = g.get("topicPolicy", {}).get("topics", [])
            denied = [t["name"] for t in topics if t.get("type") == "DENY"]
            if len(denied) >= 3:
                ok(f"{len(denied)} DENY topic(s): {', '.join(denied)}")
            else:
                bad(f"only {len(denied)} DENY topic(s), expected 3")

            words = g.get("wordPolicy", {}).get("managedWordLists", [])
            if any(w.get("type") == "PROFANITY" for w in words):
                ok("managed profanity word list enabled")
            else:
                bad("profanity word list missing")

            bi = g.get("blockedInputMessaging") or g.get("blockedInputsMessaging") or ""
            bo = (g.get("blockedOutputsMessaging")     # official spelling (plural s)
                  or g.get("blockedOutputMessaging")   # tolerate the singular form
                  or "")
            if bi and bo:
                ok("blocked input + output messages set")
                print(f"        {DIM}input : {bi[:70]}{RESET}")
                print(f"        {DIM}output: {bo[:70]}{RESET}")
            elif bi or bo:
                warn("only one blocked message set", f"input={bool(bi)} output={bool(bo)}")
            else:
                warn("blocked messages not returned by the API",
                     "verify in console: Guardrails -> your guardrail -> Blocked messages")
        except StopIteration:
            pass
        except ClientError as exc:
            msg = exc.response.get("Error", {}).get("Message", "")
            if "cancel-cred" in msg:
                bad("guardrail read denied - lab session credential grant cancelled")
            else:
                bad(f"cannot read guardrail: {exc}")

    # ---------------- runtime ----------------
    head("2. AgentCore Runtime")
    arn = env.get("AGENTCORE_RUNTIME_ARN") or _safe_config("AGENTCORE_RUNTIME_ARN")
    if not arn:
        bad("AGENTCORE_RUNTIME_ARN empty in .env", "copy it from the deploy output")
        runtime = None
    else:
        ok(f"ARN: {arn}")
        ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
        try:
            _r = ctl.get_agent_runtime(agentRuntimeId=arn.split("/")[-1])
            runtime = _r.get("agentRuntime") or _r          # flat or wrapped
        except ClientError as exc:
            msg = exc.response.get("Error", {}).get("Message", "")
            if "cancel-cred" in msg:
                bad("runtime read denied - lab session credential grant cancelled")
            else:
                bad(f"cannot read runtime: {exc}")
            runtime = None

    if runtime:
        status = runtime.get("status")
        ok(f"status = {status}") if str(status).upper().endswith("READY") else warn(
            f"status = {status}", "wait a minute and re-run")

        net = runtime.get("networkConfiguration", {}) or {}
        mode = net.get("networkMode") or runtime.get("networkMode")
        ok(f"network = {mode}") if str(mode).upper() == "PUBLIC" else bad(
            f"network = {mode}, expected PUBLIC")

        proto = runtime.get("protocolConfiguration", {}) or {}
        server = proto.get("serverProtocol") or runtime.get("protocol")
        ok(f"protocol = {server}") if str(server).upper() in ("HTTP", "HTTP_") else warn(
            f"protocol = {server}", "expected HTTP")

        role = runtime.get("roleArn") or ""
        ok(f"execution role: ...{role[-45:]}") if role else bad("no execution role")

        raw_envs = runtime.get("environmentVariables") or {}
        if isinstance(raw_envs, dict):
            envs = {k: str(v) for k, v in raw_envs.items()}
        else:
            envs = {}
            for _e in raw_envs:
                if isinstance(_e, dict):
                    envs[_e.get("name", "?")] = str(_e.get("value", ""))
                else:
                    envs[str(_e)] = ""
        want_env = ["AWS_REGION", "PROJECT_NAME", "RETURNS_KB_ID", "SHIPPING_KB_ID",
                    "WARRANTY_KB_ID", "AGENT_LOG_GROUP", "GUARDRAIL_ID",
                    "GUARDRAIL_VERSION"]
        missing = [k for k in want_env if not envs.get(k)]
        if not missing:
            ok(f"all 8 runtime env vars present")
            for k in want_env:
                v = envs[k]
                shown = v if len(v) < 46 else v[:43] + "..."
                print(f"        {DIM}{k} = {shown}{RESET}")
            if envs.get("GUARDRAIL_VERSION", "").upper() == "DRAFT":
                bad("runtime GUARDRAIL_VERSION is DRAFT",
                    "must be the numbered version; redeploy after fixing .env")
        else:
            bad(f"runtime missing env var(s): {', '.join(missing)}",
                "re-run: python src/agent_orchestrator.py deploy")

    # ---------------- memory ----------------
    head("3. AgentCore Memory")
    ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
    try:
        memories = ctl.list_memories().get("memories", [])
    except ClientError as exc:
        memories = []
        bad(f"cannot list memories: {exc}")

    mine = [m for m in memories if "udacity_agentcore_memory" in m.get("id", "")
            or "udacity" in m.get("id", "")]
    if not mine:
        bad("no udacity memory resource found",
            "re-run: python src/agent_orchestrator.py deploy  (creates it in step 4/6)")
    for m in mine:
        _m = ctl.get_memory(memoryId=m["id"])
        detail = _m.get("memory") or _m
        ok(f"memory {m['id']}")
        st = detail.get("status")
        ok(f"status = {st}") if st == "ACTIVE" else warn(f"status = {st}")
        if detail.get("description"):
            ok("has a description")
        else:
            warn("no description set")
        strategies = detail.get("strategies", []) or []
        types = [s.get("type") for s in strategies]
        if any(t and "SUMMAR" in str(t) for t in types):
            ok(f"strategy types = {types}")
        else:
            bad(f"strategies = {types}, expected a SUMMARY strategy")
        expiry = detail.get("eventExpiryDuration")
        ok(f"eventExpiryDuration = {expiry} days") if expiry == 7 else bad(
            f"eventExpiryDuration = {expiry}, expected 7")

    # ---------------- observability ----------------
    head("4. Observability")
    log_group = env.get("AGENT_LOG_GROUP") or _safe_config("AGENT_LOG_GROUP")
    logs = boto3.client("logs", region_name=REGION)
    try:
        groups = [g["logGroupName"] for g in
                  logs.describe_log_groups(logGroupNamePrefix=log_group).get("logGroups", [])]
        if groups:
            ok(f"log group exists: {groups[0]}")
        else:
            bad(f"log group {log_group} not found", "re-run the deploy (step 5/6 creates it)")
    except ClientError as exc:
        bad(f"cannot describe log groups: {exc}")

    xray = boto3.client("xray", region_name=REGION)
    try:
        dest = xray.get_trace_segment_destination()
        status = dest.get("Destination")
        ok(f"X-Ray trace segment destination = {status}") if status == "CloudWatchLogs" else warn(
            f"X-Ray segment destination = {status}",
            "Transaction Search may not be enabled; re-run the deploy")
    except Exception as exc:
        warn(f"could not read X-Ray destination: {exc}")

    try:
        now = datetime.now(timezone.utc)
        resp = xray.get_trace_summaries(
            StartTime=now - timedelta(hours=6), EndTime=now,
            FilterExpression='service("NovaMart-Orchestrator")')
        n = len(resp.get("TraceSummaries", []))
        if n:
            ok(f"{n} NovaMart trace(s) in X-Ray from the last 6 hours")
        else:
            warn("no NovaMart traces yet",
                 "run: python src/agent_orchestrator.py test   then wait ~60s")
    except Exception as exc:
        warn(f"could not query trace summaries: {exc}")

    print()
    print("=" * 74)
    if not problems:
        print(f"  {GREEN}{BOLD}Stage 4 looks complete.{RESET}")
        print("  Next:  python src/agent_orchestrator.py test")
        print("         then CloudWatch -> X-Ray traces -> Service map (Last 5 min)")
    else:
        print(f"  {RED}{BOLD}{len(problems)} problem(s) above.{RESET}")
    print("=" * 74)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
