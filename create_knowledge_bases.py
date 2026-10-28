#!/usr/bin/env python3
"""
create_knowledge_bases.py — Stage 3, without the console wizard.

Creates the three Bedrock Knowledge Bases that power the parallel multi-agent
RAG, each backed by its own index in the S3 Vectors bucket that CloudFormation
already created:

    novamart-returns-policy-kb   -> policies/returns/   -> returns-policy-index
    novamart-shipping-policy-kb  -> policies/shipping/  -> shipping-policy-index
    novamart-warranty-policy-kb  -> policies/warranty/  -> warranty-policy-index

It also:
  * creates (or reuses) an IAM role for the knowledge bases,
  * creates one S3 data source per KB with the right prefix,
  * starts an ingestion (sync) job per KB and waits for it to finish,
  * writes RETURNS_KB_ID / SHIPPING_KB_ID / WARRANTY_KB_ID into .env,
  * runs one retrieval per KB to prove it returns passages.

Run from the project root with the .venv active:

    python create_knowledge_bases.py

Re-running is safe: existing KBs are detected by name and reused.
"""

import json
import os
import re
import sys
import time
import uuid

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBED_DIMENSIONS = 1024
KB_ROLE_NAME = "udacity-agentcore-kb-role"

# (kb name, s3 prefix, vector index name, data source name, .env key)
KB_SPECS = [
    ("novamart-returns-policy-kb",  "policies/returns/",
     "returns-policy-index",  "returns-policy-docs",  "RETURNS_KB_ID"),
    ("novamart-shipping-policy-kb", "policies/shipping/",
     "shipping-policy-index", "shipping-policy-docs", "SHIPPING_KB_ID"),
    ("novamart-warranty-policy-kb", "policies/warranty/",
     "warranty-policy-index", "warranty-policy-docs", "WARRANTY_KB_ID"),
]

GREEN, RED, YELLOW, DIM, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"


def ok(msg):
    print(f"  {GREEN}OK{RESET}    {msg}")


def warn(msg):
    print(f"  {YELLOW}WARN{RESET}  {msg}")


def fail(msg, detail=""):
    print(f"  {RED}FAIL{RESET}  {msg}")
    if detail:
        print(f"        {DIM}{detail}{RESET}")


def head(msg):
    print()
    print("=" * 70)
    print(f"  {msg}")
    print("=" * 70)


def main():
    import config  # noqa: E402  (imported late so errors read clearly)

    policy_bucket = config.POLICY_BUCKET
    vector_bucket = config.VECTOR_STORE_BUCKET

    sts = boto3.client("sts", region_name=REGION)
    account = sts.get_caller_identity()["Account"]

    head("0. Target resources")
    print(f"  Account        : {account}")
    print(f"  Policy bucket  : {policy_bucket}")
    print(f"  Vector bucket  : {vector_bucket}")

    vector_bucket_arn = f"arn:aws:s3vectors:{REGION}:{account}:bucket/{vector_bucket}"
    embed_model_arn = f"arn:aws:bedrock:{REGION}::foundation-model/{EMBED_MODEL_ID}"

    # ------------------------------------------------------------------
    # 1. IAM role for the knowledge bases
    # ------------------------------------------------------------------
    head("1. IAM role for the knowledge bases")

    iam = boto3.client("iam", region_name=REGION)
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }

    index_arns = [
        f"arn:aws:s3vectors:{REGION}:{account}:bucket/{vector_bucket}/index/{idx}"
        for _, _, idx, _, _ in KB_SPECS
    ]

    permissions = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeEmbeddingModel",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel"],
                "Resource": [embed_model_arn],
            },
            {
                "Sid": "ReadPolicyDocuments",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{policy_bucket}",
                    f"arn:aws:s3:::{policy_bucket}/*",
                ],
            },
            {
                "Sid": "S3VectorsAccess",
                "Effect": "Allow",
                "Action": [
                    "s3vectors:GetIndex",
                    "s3vectors:PutVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:ListVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:QueryVectors",
                ],
                "Resource": [vector_bucket_arn, f"{vector_bucket_arn}/index/*"] + index_arns,
            },
        ],
    }

    try:
        role = iam.get_role(RoleName=KB_ROLE_NAME)["Role"]
        kb_role_arn = role["Arn"]
        ok(f"reusing existing role {KB_ROLE_NAME}")
        iam.put_role_policy(
            RoleName=KB_ROLE_NAME,
            PolicyName="KnowledgeBasePolicy",
            PolicyDocument=json.dumps(permissions),
        )
        ok("refreshed inline policy")
    except iam.exceptions.NoSuchEntityException:
        try:
            role = iam.create_role(
                RoleName=KB_ROLE_NAME,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description="Service role for NovaMart Bedrock Knowledge Bases",
            )["Role"]
            kb_role_arn = role["Arn"]
            ok(f"created role {KB_ROLE_NAME}")
        except ClientError as exc:
            fail("cannot create the IAM role", str(exc))
            print("\n  Your sandbox account may not allow iam:CreateRole.")
            print("  Fall back to the console wizard (see 03_STAGE3_KNOWLEDGE_BASES.md),")
            print("  which creates the role for you.")
            return 1

        iam.put_role_policy(
            RoleName=KB_ROLE_NAME,
            PolicyName="KnowledgeBasePolicy",
            PolicyDocument=json.dumps(permissions),
        )
        ok("attached inline policy (embedding model + S3 + S3 Vectors)")
        print(f"  {DIM}waiting 15s for IAM propagation...{RESET}")
        time.sleep(15)

    # ------------------------------------------------------------------
    # 2. Knowledge bases
    # ------------------------------------------------------------------
    head("2. Creating the three Knowledge Bases")

    agent = boto3.client("bedrock-agent", region_name=REGION)

    existing = {}
    for page in agent.get_paginator("list_knowledge_bases").paginate():
        for kb in page.get("knowledgeBaseSummaries", []):
            existing[kb["name"]] = kb["knowledgeBaseId"]

    kb_ids = {}
    for name, prefix, index, ds_name, env_key in KB_SPECS:
        if name in existing:
            kb_id = existing[name]
            ok(f"{name} already exists ({kb_id})")
            kb_ids[env_key] = kb_id
            continue

        try:
            resp = agent.create_knowledge_base(
                name=name,
                description=f"NovaMart policy documents: {prefix}",
                roleArn=kb_role_arn,
                knowledgeBaseConfiguration={
                    "type": "VECTOR",
                    "vectorKnowledgeBaseConfiguration": {
                        "embeddingModelArn": embed_model_arn,
                        "embeddingModelConfiguration": {
                            "bedrockEmbeddingModelConfiguration": {
                                "dimensions": EMBED_DIMENSIONS,
                                "embeddingDataType": "FLOAT32",
                            }
                        },
                    },
                },
                storageConfiguration={
                    "type": "S3_VECTORS",
                    "s3VectorsConfiguration": {
                        "indexArn": f"{vector_bucket_arn}/index/{index}",
                    },
                },
                clientToken=str(uuid.uuid4()),
            )
            kb_id = resp["knowledgeBase"]["knowledgeBaseId"]
            ok(f"created {name} -> {kb_id}  (index: {index})")
            kb_ids[env_key] = kb_id
        except ClientError as exc:
            fail(f"could not create {name}", str(exc))
            return 1

    # wait for ACTIVE
    print(f"\n  {DIM}waiting for knowledge bases to become ACTIVE...{RESET}")
    deadline = time.time() + 300
    for name, _, _, _, env_key in KB_SPECS:
        kb_id = kb_ids[env_key]
        while time.time() < deadline:
            status = agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]["status"]
            if status == "ACTIVE":
                ok(f"{name} is ACTIVE")
                break
            if status in ("FAILED", "DELETE_UNSUCCESSFUL"):
                fail(f"{name} entered {status}")
                return 1
            time.sleep(5)
        else:
            warn(f"{name} still not ACTIVE after 5 minutes")

    # ------------------------------------------------------------------
    # 3. Data sources + sync
    # ------------------------------------------------------------------
    head("3. Data sources and ingestion (sync)")

    for name, prefix, index, ds_name, env_key in KB_SPECS:
        kb_id = kb_ids[env_key]

        # reuse an existing data source with the same name
        ds_id = None
        for ds in agent.list_data_sources(knowledgeBaseId=kb_id).get("dataSourceSummaries", []):
            if ds["name"] == ds_name:
                ds_id = ds["dataSourceId"]
                ok(f"{name}: reusing data source {ds_name}")
                break

        if ds_id is None:
            try:
                resp = agent.create_data_source(
                    knowledgeBaseId=kb_id,
                    name=ds_name,
                    dataSourceConfiguration={
                        "type": "S3",
                        "s3Configuration": {
                            "bucketArn": f"arn:aws:s3:::{policy_bucket}",
                            "inclusionPrefixes": [prefix],
                        },
                    },
                )
                ds_id = resp["dataSource"]["dataSourceId"]
                ok(f"{name}: created data source {ds_name} (prefix {prefix})")
            except ClientError as exc:
                fail(f"{name}: could not create data source", str(exc))
                return 1

        try:
            job = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
            job_id = job["ingestionJob"]["ingestionJobId"]
            print(f"  {DIM}sync started for {name}...{RESET}", end="", flush=True)
        except ClientError as exc:
            fail(f"{name}: could not start sync", str(exc))
            return 1

        deadline = time.time() + 900
        last = None
        while time.time() < deadline:
            status = agent.get_ingestion_job(
                knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
            )["ingestionJob"]["status"]
            if status != last:
                print(f" {status}", end="", flush=True)
                last = status
            if status == "COMPLETE":
                stats = agent.get_ingestion_job(
                    knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
                )["ingestionJob"].get("statistics", {})
                print()
                ok(f"{name}: sync COMPLETE "
                   f"(indexed {stats.get('numberOfDocumentsScanned', '?')} scanned, "
                   f"{stats.get('numberOfNewDocumentsIndexed', '?')} new)")
                break
            if status in ("FAILED", "STOPPED"):
                print()
                fail(f"{name}: sync {status}")
                return 1
            time.sleep(10)
        else:
            print()
            warn(f"{name}: sync did not finish within 15 minutes")

    # ------------------------------------------------------------------
    # 4. .env
    # ------------------------------------------------------------------
    head("4. Writing KB IDs into .env")

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        warn(".env not found - creating it from .env.example")
        with open(os.path.join(os.path.dirname(env_path), ".env.example")) as fh:
            template = fh.read()
        with open(env_path, "w") as fh:
            fh.write(template)

    with open(env_path) as fh:
        lines = fh.readlines()

    for key, value in kb_ids.items():
        pattern = re.compile(rf"^{key}\s*=.*$", re.MULTILINE)
        replacement = f"{key}={value}\n"
        text = "".join(lines)
        if pattern.search(text):
            text = pattern.sub(replacement, text)
        else:
            text += replacement
        lines = text.splitlines(keepends=True)

    with open(env_path, "w") as fh:
        fh.writelines(lines)
    ok(f"updated {env_path}")

    for key, value in kb_ids.items():
        print(f"    {key}={value}")

    # ------------------------------------------------------------------
    # 5. Prove retrieval works
    # ------------------------------------------------------------------
    head("5. Retrieval check (top match per Knowledge Base)")

    runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)
    query = "What is the return window for premium customers?"
    all_ok = True

    for name, _, _, _, env_key in KB_SPECS:
        kb_id = kb_ids[env_key]
        try:
            resp = runtime.retrieve(
                knowledgeBaseId=kb_id,
                retrievalQuery={"text": query},
                retrievalConfiguration={
                    "vectorSearchConfiguration": {"numberOfResults": 1}
                },
            )
            results = resp.get("retrievalResults", [])
            if results:
                text = results[0].get("content", {}).get("text", "").replace("\n", " ")
                score = results[0].get("score", 0)
                ok(f"{name}: {len(results)} passage(s), top score {score:.3f}")
                print(f"        {DIM}{text[:150]}...{RESET}")
            else:
                fail(f"{name}: returned ZERO passages")
                all_ok = False
        except ClientError as exc:
            fail(f"{name}: retrieval error", str(exc))
            all_ok = False

    print()
    print("=" * 70)
    if all_ok:
        print(f"  {GREEN}Stage 3 complete.{RESET}")
        print("  Next:  python config.py          (KB IDs should now appear)")
        print("         python tests/test_agent.py task5")
    else:
        print(f"  {RED}Some Knowledge Bases returned nothing.{RESET}")
        print("  Most likely: an empty S3 prefix. Check with:")
        print(f"    aws s3 ls s3://{policy_bucket}/policies/ --recursive")
    print("=" * 70)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
