# V4: matched-quota multi-deployment experiment

V4 compares **one GPT-5 mini deployment at 150K TPM / 150 RPM** with
**three deployments at 50K TPM / 50 RPM each**. Both arms use five workers,
the same 65 synthetic records, unchanged complex-v2 Markdown/schema, low
reasoning, a 2,048-token output ceiling, and the same routing/retry code.

This tests a hypothesis about distributing load. **Three deployment names do
not create three times the quota.** This is online inference orchestrated in
Azure, not the asynchronous Azure Batch API.

Read the [measured pilot report](v4-results.md) and
[interactive evidence page](https://patmeh1.github.io/azure-llm-batch-starter/v4.html).
The larger [1,700-record scenario](../scenarios/v4-matched-quota-1700.json)
is provided but **was not executed**.

The later V5 upgrade changes the demo's live control allocation to 100K and
adds a fifth target. The V4 numbers remain historical evidence, not a live
configuration claim. Reproduction requires the original 150K control and
three 50K pool targets in an appropriately allocated environment.

## What stays the same, and what changes

| Variable | Single arm | Pool arm |
|---|---|---|
| Model / build | GPT-5 mini `2025-08-07` | Same |
| SKU / geography | DataZoneStandard / US Data Zone | Same |
| Resource region | South Central US | Same |
| Model targets | 1 | 3, excluding the control |
| Per-target allocation | 150K TPM / 150 RPM | 50K TPM / 50 RPM |
| Active aggregate allocation | 150K TPM / 150 RPM | 150K TPM / 150 RPM |
| Online workers | 5 sequential-per-worker streams | Same |
| Records | 65, seed 42 | Identical records |
| Prompt | 425 routes, 850 examples, 13 scenario families | Identical bytes/schema |
| Retry budget | 4 total attempts per record | 4 total across all targets |
| Request timeout | 90 seconds | Same |
| Execution | Azure Durable Functions / Scheduler | Same |
| Proactive TPM pacing | Disabled for this observation test | Same |

Both use round robin among eligible targets. In the single arm only the
control is eligible. The arms run serially, with a 65-second pause between
them. The subscription has 300K allocated across all four deployments, but
only 150K is targeted by each arm. No unrelated traffic is coordinated by
this application's router.

The interrupted v3 local run is not resumed or treated as a completed
performance baseline. V4 is a new bounded pilot, not an exact reproduction
of an unknown production configuration.

## 1. Prepare an authorized Azure environment

Use Python 3.11+ (3.12 for the deployed worker), Azure CLI, Azure Developer
CLI, and the [base Azure tutorial](azure-demo.md). Sign in to your own
subscription and confirm geography, network policy, access, and costs.
Keep the base GPT-5.4 mini deployments if you want to retain v1/v2.

```powershell
git clone https://github.com/patmeh1/azure-llm-batch-starter.git
Set-Location azure-llm-batch-starter
python -m pip install -e ".[dev,cloud]"
az login
az account set --subscription YOUR_APPROVED_SUBSCRIPTION_ID
az account show --query "{name:name,id:id}" --output json
azd env get-values
```

For a new environment, complete the base tutorial before continuing.
For an existing environment, verify that AZD targets that environment rather
than creating another one. Do not copy another operator's `.azure` directory.
The fixed v4 experiment currently requires `southcentralus`; US Data Zone
inference is not guaranteed to stay in that resource region alone.

## 2. Verify model availability and available quota

Get the approved names from your environment, not this public report:

```powershell
$rg = azd env get-value AZURE_RESOURCE_GROUP
$account = azd env get-value AZURE_OPENAI_ACCOUNT
$resourceId = az cognitiveservices account show --resource-group $rg --name $account --query id --output tsv
az cognitiveservices account list-models --resource-group $rg --name $account --output json
az cognitiveservices usage list --location southcentralus --output json
az cognitiveservices account deployment list --resource-group $rg --name $account --output json
```

Confirm GPT-5 mini `2025-08-07` and **DataZoneStandard** are available.
This experiment needs 300K total allocated capacity: a retained 150K control
and 150K available for the three new children. Do not remove another workload's
allocation to make the experiment fit.

The runtime map expects these generic deployment names:

| Public metric alias | Deployment name | Capacity units |
|---|---|---|
| control | `gpt5-mini-online` | 150 |
| pool-1 | `gpt5-mini-v4-1` | 50 |
| pool-2 | `gpt5-mini-v4-2` | 50 |
| pool-3 | `gpt5-mini-v4-3` | 50 |

The units corresponded to 150K/150 and 50K/50 in the tested configuration.
Verify both allocations and returned rate-limit headers in your environment;
RPM-to-TPM ratios vary by model.

## 3. Deploy the additive model children

If the control does not already exist, follow the
[v3 deployment guide](v3-gpt5-mini.md) using `infra\v3-model.bicep`.
If it exists, inspect its exact model, version, SKU, and capacity; do not
silently replace it.

Preview the v4 changes:

```powershell
az deployment group validate --resource-group $rg --template-file infra\v4-models.bicep --parameters accountName=$account
az deployment group what-if --resource-group $rg --template-file infra\v4-models.bicep --parameters accountName=$account
```

For a first deployment, expect **exactly three new model children** and no
modification or deletion of existing resources. Review discrepancies before
proceeding. The template sequences child writes to avoid account-level
deployment write conflicts.

After quota, costs, and the plan are approved:

```powershell
az deployment group create --name v4-matched-gpt5-mini --resource-group $rg --template-file infra\v4-models.bicep --parameters accountName=$account --mode Incremental
az cognitiveservices account deployment list --resource-group $rg --name $account --output json
azd deploy api --no-prompt
python scripts\verify-shared-package.py
azd show
```

This does not reapply the base infrastructure or change its network controls.
The existing managed identity needs account-scoped OpenAI access, Blob/Table
access, and Durable Task Scheduler permissions. No model API key is required.
`host.json` and the starter use workflow version 4.0; older workflow sequences
are preserved.

## 4. Prepare private configuration and preview the pilot

If `.azure\v3-config.json` already exists, inspect and reuse it. Otherwise:

```powershell
llm-batch v3-config .azure\demo-config.json --output .azure\v3-config.json
```

That command only creates a private configuration; it does not deploy or run
inference. The v4 experiment pins its model, routing map, reasoning, timeout,
and generation settings regardless of the older base model defaults.

With application version 0.5.0 or later, live online experiments also require
the account resource ID for actual topology validation. Use the configuration
helper to create a separate private file, without altering a checkpointed v3
configuration:

```powershell
llm-batch v5-config .azure\demo-config.json --resource-id $resourceId --output .azure\v4-config.json
```

The helper only writes configuration; `v4-run` still selects the V4 experiment.
Use `--config .azure\v4-config.json` for the live commands below. If the
allocation has been changed for V5, preflight refuses a misleading V4 replay.

```powershell
llm-batch v4-run --scenario scenarios\v4-matched-quota-pilot.json --config .azure\v4-config.json
```

Without `--yes`, this is a dry run. The published pilot preview estimated
**32,334-32,622 input tokens per record** and **$6.9867** for both arms at
the four-attempt conservative ceiling, inside a **$10** model-usage guard.
Each arm reserves at most $5 and 260 attempts, with a two-hour admission
deadline. These are application controls, not Azure spending caps.

The fixed estimates use USD/million tokens: input 0.275, cached input 0.0275,
output 2.2. Confirm current prices before running; infrastructure, taxes,
missing usage, and billing adjustments are not included. The tokenizer
estimate plus output ceiling is a conservative application reservation,
not a claim about the service's exact admission algorithm.

## 5. Start once; monitor the same ID

After reviewing the plan and approving charges:

```powershell
llm-batch v4-run --scenario scenarios\v4-matched-quota-pilot.json --config .azure\v4-config.json --yes
llm-batch v4-status YOUR_RUN_ID --config .azure\v4-config.json --wait --timeout 7200
```

Save the returned root ID. The workflow runs in Azure and continues if the
local observer disconnects. The observer writes
`outputs\v4\YOUR_RUN_ID\summary.json`. A monitor timeout does not cancel
Azure work; reconnect with `v4-status` on the same ID, not `v4-run --yes`.

Exit 0 means the final acceptance gate passed. Exit 1 means completed findings,
failures, or a semantic review requirement. Exit 2 means still pending.
A Durable runtime state of `Completed` alone does not mean every record
succeeded or the answers were correct. Worker progress counts processed
records, including failures; it is not a success count.

The local dashboard can display the same authenticated scheduler metadata:

```powershell
llm-batch dashboard --config .azure\v4-config.json
```

Select the scheduler source, not local fixture history. It separates arm
outcomes, per-target responses/429s, summed routing wait, and semantic gates.

## 6. Understand retries, durable state, and cancellation

- The OpenAI SDK has `max_retries=0`. There is one application retry layer.
- HTTP 429 and 5xx can retry within **four total attempts across all targets**.
  `retry-after-ms` takes precedence over `Retry-After` seconds or HTTP dates.
  Without valid guidance, fallback is 30 seconds, then 60, then 120, plus
  0-3 seconds of jitter.
- A retryable response cools down its deployment, not every deployment.
  A record can use another healthy target immediately. If all targets are
  cooling, a durable timer waits and checks cancellation/deadline again.
- An exclusive entity lease prevents concurrent v4 experiments in this task
  hub. It does not reserve the subscription against unrelated applications.
- Each paid attempt has an atomic private Blob intent and response.
  Existing responses reconcile without another inference. An intent without
  a response becomes `ambiguous_online_result`, not permission to resend.
  Connection/timeouts are also treated conservatively as ambiguous.
- Fatal request/auth/model errors stop new work. Confirmed terminal results
  release the routing lease; ambiguous results keep it held for investigation.

Request cancellation with:

```powershell
llm-batch azure-cancel YOUR_RUN_ID --config .azure\v4-config.json --yes
```

Cancellation stops new attempts when workers next check the saved flag.
It cannot recall an already issued online request. Continue observing until
all records reconcile or the workflow explicitly requires investigation.

Do not use `azure-retry-failed` or `azure-recover` for v4: those are Batch
operations and the adapter rejects v4 roots. For an orchestration failure,
inspect native history and saved intents/responses, correct the specific
runtime problem, and establish the status of any in-flight call before an
operator-approved recovery. Do not delete the entity or create a replacement
paid root to hide ambiguity.

## 7. Export and interpret the evidence

After the final report is saved:

```powershell
llm-batch v4-export outputs\v4\YOUR_RUN_ID\summary.json outputs\v4-public
```

The public export contains aggregate arm/target measurements and the first
three **synthetic** output examples per arm. Full JSONL, twelve-column CSV,
analysis CSV, manifests, prompts, and per-attempt artifacts remain in private
Blob Storage under the root/stage prefixes. Retrieve those only through an
authorized network route and identity; do not relax network controls to
download them.

Inspect:

| Measurement | Interpretation |
|---|---|
| Validated / failed / pending | Must reconcile to 65 per arm; HTTP 200 alone is insufficient |
| Whole workflow versus arm wall time | Workflow includes bootstrap and inter-arm pause; arm time includes durable overhead |
| Successful record elapsed | Includes retries and waits; excludes records that ultimately failed |
| HTTP 200 p50/p95 | Successful HTTP-attempt latency, not end-to-end record latency |
| HTTP 429 attempts | Not the same as failed records; a record can eventually recover |
| Router wait | Sum across records/workers; can exceed elapsed wall time |
| Input/cached/output/reasoning tokens | API usage; cached/reasoning counts are subsets, not additive totals |
| Semantic gate | Unchanged gold fields/rules; a valid JSON schema is not acceptance |
| Returned limits / error indication | Observations, not a direct backend scale-up-state signal |

The single arm runs first; pool endpoints are new, and cache/service
conditions can differ. This is one pilot, not a randomized repeated benchmark.
Retain failures and unknowns. Do not rank models using different sample
sizes or use the interrupted v3 run as a completed baseline.

## 8. Preview the larger scenario; do not launch by accident

```powershell
llm-batch v4-run --scenario scenarios\v4-matched-quota-1700.json --config .azure\v4-config.json
```

This provides 1,700 records per arm, five streams of 340, a 12-hour admission
deadline per arm, four attempts per record, and a $200 combined model guard.
The conservative preview is approximately $182.73 at the configured rates.
**It was not executed in this validation.** A separate explicit cost/time
decision is required before adding `--yes`; do not extrapolate pilot
throughput or claim 1,700-record success.

## What to do about 429s and scaling

Use this experiment to decide what to investigate, not to promise that
multiple deployments eliminate throttling. Inspect service guidance, actual
limits, per-request demand, cache behavior, and unrelated traffic. For normal
online work, use shared, per-target request/token admission with headroom in
addition to bounded retries. Splitting 150K into 50K pieces can reduce the
headroom available to an individual long request.

If more sustained throughput is required, review prompt reduction and its
quality impact, approved capacity/quota, a supported asynchronous Batch SKU,
or a separately evaluated provisioned-throughput design. None of those was
silently substituted into v4.

Official references:

- [Quota allocation and per-deployment limits](https://learn.microsoft.com/azure/foundry/openai/how-to/quota).
- [Interpreting Azure OpenAI HTTP 429](https://learn.microsoft.com/microsoft-cloud/dev/dev-proxy/concepts/azure-openai-429-token-rate-limit).
- [Durable orchestration versioning](https://learn.microsoft.com/azure/durable-task/common/durable-orchestration-versioning).

Documentation can describe admission estimates differently across model/API
generations. Treat input-plus-output reservation as this application's
conservative budgeting method; use measured headers and usage rather than
asserting a universal `max_tokens` enforcement rule.
