# V5: quota-aware scheduling and measured request completion

V5 compares two policies on the **same five GPT-5 mini deployments**, with
**520 synthetic records per arm** and a shared five-worker ceiling.
The model remains GPT-5 mini `2025-08-07`, US DataZoneStandard, in the
South Central US resource region. The complex-v2 Markdown/schema and
semantic grading rules are unchanged.

The five targets have **100K + 50K + 50K + 50K + 50K = 300K TPM**
and corresponding total 300 RPM. The reactive arm is a V4-style eligible-target
round-robin policy running in the V5 harness, not a replay of the historical
65-record V4 experiment. Both new arms receive the same input dataset,
client reuse, manifest caching, durable shared queue, and concurrency limits.
Only admission/selection policy differs.

## What the scheduler does

- **Reactive:** choose the next idle, non-cooling target; react to service
  throttling with shared per-target cooldowns.
- **Quota-aware:** reserve estimated input plus output allowance and a request
  slot before dispatch, using 95% of configured limits by default. Choose an
  eligible target using available capacity and observed successful-call latency.
- One in-flight request per target prevents out-of-order budget feedback.
  Returned remaining/limit headers adjust the budget within the configured
  allocation. Missing headers use configured estimates; invalid values are
  counted explicitly. Temporary lower observed limits expire after 60 seconds
  so bounded traffic can recheck recovery; this is not a scale-up-state API.
- Retries honor service guidance. The fallback is 30-second exponential
  backoff plus bounded jitter. There are at most four attempts per record,
  across all targets, and SDK retries are disabled.
- Waiting uses durable timers and an entity-owned queue. Any free worker can
  process the next eligible record; work is not stranded in fixed groups.

This is **online inference, not the Azure Batch API**. Application estimates
are not the service's exact token accounting, and no policy guarantees zero
429s or greater throughput under every workload.

## Deployment and prerequisites

Start with the [Azure tutorial](azure-demo.md), then the model setup in the
[V4 guide](v4-multi-deployment.md). The four GPT-5 mini deployments must
already exist; running the older paid experiment is not required.
This is a scoped upgrade template, not a clean-account five-model deployment.
Existing v1/v2 model resources,
managed identities, Storage protection, and Durable Task Scheduler are reused.
V5's scoped template changes only the original GPT-5 mini control from 150
to 100 capacity units and adds `gpt5-mini-v5-5` at 50. The three v4 pool
deployments stay at 50 each. No quota increase, new region, or PTU is required
for this 300K allocation.

This changes V4's live control allocation. Preserve its historical evidence;
do not label a later 100K control run as the original 150K experiment.
V4/V5 live starters now require a private `model_resource_id` and verify
actual model, version, SKU, account/endpoint binding, region, and allocation
before scheduling. The V5 worker checks again before each stage.

```powershell
$rg = azd env get-value AZURE_RESOURCE_GROUP
$account = azd env get-value AZURE_OPENAI_ACCOUNT
$resourceId = az cognitiveservices account show --name $account --resource-group $rg --query id --output tsv
$policy = az cognitiveservices account deployment show --name $account --resource-group $rg --deployment-name gpt5-mini-online --query properties.raiPolicyName --output tsv
az deployment group validate --resource-group $rg --template-file infra\v5-models.bicep --parameters accountName=$account raiPolicyName=$policy
az deployment group what-if --resource-group $rg --template-file infra\v5-models.bicep --parameters accountName=$account raiPolicyName=$policy
```

Review the change set: one reduction and one new deployment, with no deletion
or change to other models. After approving the allocation change and costs:

```powershell
az deployment group create --name v5-adaptive-models --resource-group $rg --template-file infra\v5-models.bicep --parameters accountName=$account raiPolicyName=$policy --mode Incremental
azd deploy api --no-prompt
python scripts\verify-shared-package.py
llm-batch v5-config .azure\demo-config.json --resource-id $resourceId --output .azure\v5-config.json
```

Do not overwrite an existing private configuration without inspecting it.
The worker and starter use workflow version 5.0; older workflow sequences
remain supported. Keep the existing identity and network boundaries intact.

## Preview, then start once

```powershell
llm-batch v5-run --scenario scenarios\v5-adaptive-520.json --config .azure\v5-config.json
```

This only previews. There are 1,040 benchmark evaluations and ten warm-up
records, with a maximum 4,200 model attempts including retries. The $75
configured model-usage guard includes warm-ups but excludes infrastructure
and is not an Azure billing cap. Check current prices before execution.
V5 currently bounds workloads to 520 records per arm.

After review and cost approval, add `--yes`, save the returned root ID,
and monitor that same ID:

```powershell
llm-batch v5-run --scenario scenarios\v5-adaptive-520.json --config .azure\v5-config.json --yes
llm-batch v5-status YOUR_RUN_ID --config .azure\v5-config.json --wait --timeout 15000
```

Each arm has a two-hour admission deadline and a 90-second per-call timeout.
Each warm-up stage sends one distinct synthetic record to each target with
bounded retries and a five-minute admission deadline. A 65-second pause
follows warm-ups and separates arms. Warm-up failures cancel subsequent
inference rather than silently benchmarking an incomplete pool. Durations
and costs for warm-ups are reported separately.

The workflow runs in Azure after the local observer exits. Observer timeout
does not cancel it. Exit 0 means final acceptance passed, 1 means findings or
failure, and 2 means still pending. Do not start a new root merely because
the observer disconnected.

The waiting observer can reconnect up to three consecutive times for
transient gRPC unavailability/deadline errors, retaining its original
monitor deadline and the same run ID. It logs each reconnection; authentication
or permission errors are not retried as success. This retries metadata reads
only, never orchestration creation or model inference.

```powershell
llm-batch azure-cancel YOUR_RUN_ID --config .azure\v5-config.json --yes
llm-batch v5-export outputs\v5\YOUR_RUN_ID\summary.json outputs\v5-public
```

Cancellation stops new admissions; it cannot recall an in-flight model call.
The shared V4/V5 experiment lease prevents overlapping application runs in
this task hub. Atomic paid intents/responses prevent blind retransmission.
Confirmed responses still reconcile after a deadline or cancellation;
uncertain outcomes keep the lease held for investigation. Batch-specific
recovery/retry commands reject these online experiment roots.

## Read the averages correctly

| Metric | What is actually timed |
|---|---|
| Mean HTTP 200 duration | Successful client API attempts, including network/auth/SDK time; not server compute alone |
| Mean all-HTTP duration | Every measured attempt, including fast 429s; never use this alone as a speed claim |
| Mean successful record processing | First ready-at-queue-head timestamp through a validated final outcome; includes admission/retries but excludes initial backlog |
| Mean all-record processing | Same timing across successes and failures, with its own denominator |
| Mean queue-to-completion | Arm enqueue time through each record's final outcome; includes backlog waiting |
| Whole arm elapsed | Start of the measured arm through final reconciliation |
| Validated records/minute | Successful records divided by full arm time; a throughput metric, not request latency |

Record completion timestamps are captured when the outcome is ready for its
final metadata write, after response persistence and validation. HTTP response
timestamps remain separately available in private artifacts. Neither metric
is derived by dividing job duration by record count. Means, p50/p95, measured
counts, failures, and waiting are published separately.

## Interpretation and boundaries

The target is useful throughput and complete reconciliation, not merely a
shorter failed run. If a mode fails records, its time to complete all 520
successfully is not established. Semantic acceptance remains independent:
unchanged prompts can produce valid JSON with incorrect analytical fields.

The new 300K pool is not directly comparable with V4's 150K-per-arm timings
or 65-record counts. Warm-ups do not guarantee identical cache state, and
one paired run is not a randomized repeated benchmark or performance SLA.
Unrelated applications can still consume the same allocations.

Only allowlisted aggregate measurements and bounded synthetic examples
belong in public artifacts. Full manifests, records, response bodies, request
IDs, and account context remain in private Azure Storage.
