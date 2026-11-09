# V3: GPT-5 mini sequential versus five client-side groups

V3 keeps the **v2 complex Markdown prompts and output schema unchanged**, but
uses **GPT-5 mini `2025-08-07` in US DataZoneStandard**. It compares 1,700
sequential online requests with the same 1,700 records partitioned into five
concurrent, sequential-per-group streams of 340.

**`client-batch` is not the Azure asynchronous Batch API.** The nine checked
public US regional catalogs did not list GPT-5 mini DataZoneBatch, and the
capacity endpoint returned no entries for that model/version/SKU. The chosen
reproduction therefore uses the online endpoint in both modes. Existing v2
GPT-5.4 mini online and service-Batch deployments remain separate.

This matches a common client grouping/throttling pattern, not an exact
production environment. Quota, retries, timeouts, transcript distribution,
output budgets, network path, VM behavior, and competing traffic can differ.
Do not promise a particular 10-second or 49-second latency.

## What stays the same and what changes

| Setting | V3 |
|---|---|
| Markdown prompts | Same four packaged files as v2; complex system/taxonomy/user content unchanged |
| Complex prompt/schema hash | `469532c76922192658dd12a81cd842a8b2328d529db6acbb4c433385e30cb4fe` |
| Model / version | GPT-5 mini / `2025-08-07` |
| Resource region | South Central US in the measured deployment |
| Deployment type | `DataZoneStandard`, US processing geography |
| Online allocation | 150,000 TPM / 150 RPM, verified in deployment metadata and response headers |
| Dataset | 1,700 synthetic complex conversations, seed 42, identical between modes |
| Grouping | One stream, then five fixed contiguous streams of 340; no dynamic work stealing |
| Generation | Low reasoning effort, 2,048-token output ceiling |
| Initial request pacing | Deliberately unpaced; the service enforces its shared capacity |
| Retry policy | At most three attempts per record; honor service retry guidance, bounded fallback |
| Limits per mode | At most 5,100 attempts, 10 hours of admission wall time, USD 75 conservative inference reservation |
| Total scenario guard | USD 150 estimated model usage; not a full infrastructure billing cap |

The 150K allocation and retry settings are **test assumptions**, not evidence
of another environment's configuration. The actual prompts remain synthetic:
they reproduce hierarchical/rule complexity without including private wording
or customer records.

The runner executes on the client machine, as a typical VM script would.
It is **not** submitted as an Azure service-Batch job. The model executes in
Azure. Keep the runner host awake and connected; checkpoints allow explicit
recovery after interruption.

## Availability check

Checked on 2026-11-09 for GPT-5 mini `2025-08-07`:

| Public US regions | Model listed | DataZoneStandard | DataZoneBatch |
|---|---|---|---|
| East US, East US 2, Central US, North Central US, South Central US, West US, West US 3 | Yes | Yes | Not listed |
| West US 2, West Central US | No | Not listed | Not listed |

Availability can change and is subscription-dependent. Recheck before deploying.
Retail pricing contains Batch meters for the model, but **a pricing meter is
not proof that the corresponding deployment SKU is available**.

## Add a separate deployment without replacing v2

Use your own existing AI Services account in an approved US region. Do not copy
someone else's account identifiers or reuse another workload's deployment name.
Sign in and select the intended subscription first.

```powershell
$subscription = az account show --query id --output tsv
python scripts\preflight.py --subscription $subscription --region southcentralus --model gpt-5-mini --version 2025-08-07 --online-capacity 150 --online-only
```

The `--online-only` preflight deliberately does not require or imply Batch
support. For an already deployed model, inspect its actual SKU/version/limits
instead of requesting a second quota allocation.

Set these variables to your own dedicated resources:

```powershell
$group = "YOUR_RESOURCE_GROUP"
$account = "YOUR_EXISTING_AI_ACCOUNT"
az deployment group what-if --resource-group $group --template-file infra\v3-model.bicep --parameters accountName=$account capacity=150
```

Review the preview: it should create only `gpt5-mini-online`. After approving
the target, cost, and residency:

```powershell
az deployment group create --resource-group $group --mode Incremental --template-file infra\v3-model.bicep --parameters accountName=$account capacity=150
az cognitiveservices account deployment show --resource-group $group --name $account --deployment-name gpt5-mini-online
```

The template reuses the existing account; it does not create permissions,
network exceptions, Global deployments, or provisioned capacity. Data-plane
inference still requires the appropriate account-scoped Entra role.

## Create the separate private configuration

Install the application from the source repository:

```powershell
python -m pip install -e ".[dev,cloud]"
llm-batch v3-config .azure\demo-config.json --output .azure\v3-config.json
```

The new configuration points to the separate model deployment and leaves the
v2 configuration untouched. It refuses to overwrite an existing file.
For a different existing endpoint, supply an appropriate private base Settings
file. Never commit the resulting `.azure` configuration.

Configured retail estimates per million tokens, checked 2026-11-09:
input USD 0.275, cached input USD 0.0275, output USD 2.2. Review current
[Azure retail pricing](https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview)
before running. Client-side groups receive **no Batch API price discount**.

## Preview and run the full experiment

```powershell
llm-batch v3-run --scenario scenarios\v3-gpt5-mini-1700.json --config .azure\v3-config.json
```

Preview makes no inference calls. For the shipped dataset/rates it estimates
32,331-32,631 input tokens per request, USD 22.84 per mode at the first-attempt
ceiling, and **USD 137.05** across both modes with three attempts each and
uncached input pricing. Actual billed usage can be lower or differ; these
estimates exclude infrastructure.

After approving the experiment:

```powershell
llm-batch v3-run --scenario scenarios\v3-gpt5-mini-1700.json --config .azure\v3-config.json --yes
```

The command prints the checkpoint path after initialization. This experiment
can take many hours. Do not start it a second time to check progress.
Successful responses must expose the expected 150K TPM / 150 RPM limits;
a mismatch stops the comparison rather than silently testing another capacity.

The ordinary short `stress.py` guard remains unchanged. The full reproduction
uses a separate explicitly bounded profile, restricted to the pinned model,
at most 1,700 records, three attempts, five workers, and the stated time/cost
limits.

## Monitor, resume, and export

Use the printed checkpoint path:

```powershell
llm-batch v3-status outputs\v3\EXPERIMENT\experiment.json
llm-batch dashboard --state outputs\v3\EXPERIMENT\state.sqlite3
```

Select **Local** in the dashboard. Both modes use the same local experiment
database, making the comparison independent of the cloud scheduler's history.
The grouped view explicitly labels its jobs as client groups, not service Batch.

If the owning process stopped, investigate before resuming:

```powershell
llm-batch v3-resume outputs\v3\EXPERIMENT\experiment.json
```

The input/configuration/prompt fingerprint must match. Completed records and
groups remain completed. Previous HTTP intents count against the original
attempt/cost budgets; the original stage deadline is not reset. Unknown
in-flight responses become explicit ambiguous failures rather than automatic
duplicate inference. Their elapsed time can be unavailable.
This is not an exactly-once inference guarantee: ordinary bounded retries after
network/service errors can still incur additional paid work.

The per-deployment file lock prevents a second local coordinator from spending
the same configured deployment budget. It is not a distributed limiter and
does not control unrelated traffic from other machines.

Export an allowlisted snapshot without endpoints or Azure identifiers:

```powershell
llm-batch v3-export outputs\v3\EXPERIMENT\experiment.json outputs\v3-public
```

While running, `summary.json` explicitly has `results_final: false`. Once both
modes are terminal, export also writes per-mode `*-results.jsonl` and
`*-analysis.csv` containing synthetic outputs and quality checks. Review any
artifact before publishing it. Private configs, SQLite files, raw logs, and
actual resource identities must remain out of Git.
The analysis CSV leaves unavailable analytical fields blank on failed records;
use the matching JSONL status/error fields rather than treating every CSV row
as successful. `completed_with_findings` means execution ended, not that every
record or quality check passed.

## Interpret the results

Compare whole-run duration and validated-record throughput, then distinguish:

- HTTP attempt latency from full record latency including retries.
- Successful-record averages from all measured-record averages and their
  measurement counts; unknown elapsed times are not treated as zero.
- 429 attempts from final failed records and records that later recovered.
- Completed/failed/unprocessed outcomes from semantic field/rule accuracy.
- The five group lifetimes from total wall time; overlapping durations must
  not be added together.

Sequential and grouped modes share the **same service quota**. Five groups do
not create five times the TPM. This observation profile does not use the normal
shared client pacing; it measures the service response to the grouped pattern
with bounded retries. Use the existing paced online modes for a mitigation
comparison only as a separately identified experiment.

Cache state and model responses are not deterministic. The second mode may
benefit from an already-warm prefix, and a 65-second cooldown does not flush
that cache. Preserve returned cache usage and all failures. Do not force the
experiment to reproduce a particular latency or claim identical production
behavior from a synthetic test.

## Results

See [v3 results and status](v3-results.md). A report labeled pending is not a
completed benchmark. V2's results must not be relabeled as GPT-5 mini results.
