# NovaMart Multi-Agent Customer Support — Submission

Enterprise customer-support automation built with the **Strands Agents SDK** on
**Amazon Bedrock AgentCore**, with parallel multi-agent RAG over three Bedrock
Knowledge Bases, a Bedrock Guardrail, AgentCore Memory, and CloudWatch + X-Ray
observability.

---

## What was implemented

Everything lives in **`src/agent_orchestrator.py`** (the only file modified —
the provided helper modules are untouched).

### Task 2 — Multi-agent orchestration

| Agent | Model | Temperature | Tools |
|---|---|---|---|
| `OrchestratorAgent` | Claude Haiku 4.5 | 0.0 | `initialize_session`, `route_to_inventory_agent`, `route_to_policy_agent`, `route_to_refund_agent`, `route_to_communication_agent` |
| `InventoryAgent` | Claude Sonnet 4.5 | 0.1 | `check_order_status`, `get_customer_tier`, `list_customer_orders` |
| `RefundAgent` | Claude Sonnet 4.5 | 0.1 | `get_inventory_context`, `initiate_refund` |
| `PolicyAgent` | Claude Sonnet 4.5 | 0.2 | `search_all_policies` |
| `CommunicationAgent` | Claude Sonnet 4.5 | 0.3 | `get_full_workflow_context` |

- **Parallel multi-agent RAG**: `build_policy_agent()` creates three retriever
  sub-agents (`ReturnsPolicyRetrieverAgent`, `ShippingPolicyRetrieverAgent`,
  `WarrantyPolicyRetrieverAgent`, all `temperature=0.0`), each bound to one
  Knowledge Base. `search_all_policies()` fans out with
  `ThreadPoolExecutor(max_workers=3)` and `as_completed()`.
- **Shared WorkflowState**: every routing tool reads the DynamoDB record, invokes
  the specialist, then writes back with `_update_workflow_state(..., expected_version)`
  — optimistic locking, so concurrent agents cannot overwrite each other.
- **The Orchestrator never writes the customer reply**; `route_to_communication_agent`
  is always the final call.
- **Business rule**: Standard = 30-day return window, Premium = 60-day.

### Task 3 — Guardrail + runtime

- `create_guardrail()`: content filters (SEXUAL/VIOLENCE/HATE = HIGH,
  INSULTS/MISCONDUCT = MEDIUM), PII (card + SSN BLOCK, email + phone ANONYMIZE),
  three DENY topics (competitor products, pricing negotiations, legal threats),
  managed profanity list, custom blocked input/output messages.
  Uses `tierConfig STANDARD` and `crossRegionConfig us.guardrail.v1:0`, then
  publishes a **numbered version** via `create_guardrail_version()`.
- `deploy_to_agentcore_runtime()`: builds the runtime environment, calls
  `agentcore_cli.configure_runtime(network_mode='PUBLIC', protocol='HTTP', ...)`,
  `agentcore_cli.deploy()`, then reads the ARN with `deployed_runtime_arn()`.
  Refuses to deploy if any Knowledge Base ID is missing.

### Task 4 — Memory

`configure_memory()` creates an AgentCore Memory resource with
`summaryMemoryStrategy` (SESSION_SUMMARY), `eventExpiryDuration=7`, a
description and an idempotency `clientToken`.

### Task 5 — Knowledge Bases (created in AWS, not in code)

| Knowledge base | S3 prefix | Vector index |
|---|---|---|
| `novamart-returns-policy-kb` | `policies/returns/` | `returns-policy-index` |
| `novamart-shipping-policy-kb` | `policies/shipping/` | `shipping-policy-index` |
| `novamart-warranty-policy-kb` | `policies/warranty/` | `warranty-policy-index` |

All three use `amazon.titan-embed-text-v2:0` (1024 dims) and the **existing**
S3 Vectors bucket created by the CloudFormation stack — not a quick-created store.
Each data source has a completed sync.

### Task 6 — Observability

`configure_observability()` builds a `loggingConfiguration`
(`cloudWatchConfig`: INFO, enabled; `xRayConfig`: enabled, `samplingRate=1.0`)
and applies it through `apply_observability_config()` inside `try/except`.
CloudWatch Transaction Search is enabled so X-Ray receives traces from both
local runs and the deployed runtime.

---

## Evidence

All screenshots are in [`evidence/`](evidence/).

| File | Shows |
|---|---|
| `01_cfn_complete.png` | CloudFormation stack `udacity-agentcore` → CREATE_COMPLETE |
| `02_config_output.png` | `python config.py` — tables, S3 Vectors bucket + 3 indexes, role, log group |
| `03_seed_data.png` | Seeded DynamoDB data and the three S3 policy prefixes |
| `04_task2_pass.png` | `tests/test_agent.py task2` — all PASS |
| `05_kb_list.png` | Knowledge Bases list — all three, status available |
| `06_kb_returns.png` | Returns KB — prefix, Titan V2, vector bucket + `returns-policy-index`, sync complete |
| `07_kb_shipping.png` | Shipping KB — same, `shipping-policy-index` |
| `08_kb_warranty.png` | Warranty KB — same, `warranty-policy-index` |
| `09_env_populated.png` | `.env` with KB IDs, runtime ARN, guardrail ID and version |
| `10_task5_pass.png` | `tests/test_agent.py task5` — passes incl. parallel retrieval |
| `11_deploy_output.png` | Deploy output — guardrail ID/version, runtime ARN, memory ARN |
| `12_guardrail_policies.png` | Guardrail configuration in the console |
| `13_guardrail_versions.png` | Guardrail **Versions** tab — numbered version |
| `14_runtime_ready.png` | AgentCore Runtime — status READY, PUBLIC, HTTP |
| `15_runtime_env_vars.png` | Runtime configuration — all 8 environment variables |
| `16_task3_pass.png` | `tests/test_agent.py task3` |
| `17_memory.png` | AgentCore Memory — ACTIVE, SESSION_SUMMARY, 7-day expiry |
| `18_task4_pass.png` | `tests/test_agent.py task4` |
| `19_task6_pass.png` | `tests/test_agent.py task6` |
| `20_xray_service_map.png` | **X-Ray Service Map** — Orchestrator → workers + Knowledge Bases |
| `21_xray_trace_detail.png` | A single trace's timeline across agents |
| `22_e2e_scenarios.png` | `python src/agent_orchestrator.py test` — three scenarios |
| `23_workflow_state.png` | DynamoDB WorkflowState item with all agent columns populated |
| `24_test_all_pass.png` | `tests/test_agent.py all` — every task passing |

---

## Architecture

```
                        Customer request
                               │
                    OrchestratorAgent (Haiku 4.5, temp 0.0)
                               │
        ┌──────────────┬───────┴────────┬──────────────────┐
        │              │                │                  │
  InventoryAgent   PolicyAgent      RefundAgent    CommunicationAgent
   (DynamoDB)    (multi-agent RAG)   (DynamoDB)      (final reply)
                       │
            ┌──────────┼──────────┐
   ReturnsPolicyRetriever  ShippingPolicyRetriever  WarrantyPolicyRetriever
      (Bedrock KB)              (Bedrock KB)              (Bedrock KB)
            └────────── run in PARALLEL ──────────┘

        All agents read/write → DynamoDB WorkflowState (optimistic locking)
```

---

## Running it

```bash
# prerequisites
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
npm install -g @aws/agentcore@0.30.0                    # Node 20+ required
# uv is also required: https://docs.astral.sh/uv/

# infrastructure
aws cloudformation deploy --template-file infrastructure/starter_stack.yaml \
    --stack-name udacity-agentcore --capabilities CAPABILITY_NAMED_IAM --region us-east-1
python infrastructure/seed_data.py
cp .env.example .env     # then fill in the IDs

# verification
python tests/test_agent.py all
python src/agent_orchestrator.py test
```
