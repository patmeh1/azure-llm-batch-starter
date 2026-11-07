# Operations

## Run states and recovery

Local and cloud runs retain stable IDs and an explicit application status.
Success requires every expected record to reconcile to validated output.
Pending and ambiguous records are not silently relabeled failed/successful.

Local `resume` uses saved inputs/configuration. Cloud workflows persist through
host restarts in Durable Task Scheduler; the observer CLI can disconnect.
Activity/queue processing is at least once, so ambiguous remote submissions
require reconciliation rather than blind replay.

`retry-failed` and `azure-retry-failed` select confirmed transient failures
(such as HTTP 429 or 5xx) and create a new run with parent lineage. They do not
silently retry refusals, malformed output, missing/ambiguous results, or
authentication failures. Correct those conditions before creating an explicit
new run. Resume does not reissue already-terminal failed records.

Use `retry-failed` for explicitly failed local records. Cloud operator recovery
must preserve original IDs and distinguish a retryable record from an
unreconciled paid submission. Do not release held reservations merely because a
local timer elapsed.

## Rate and cost controls

- Normal online execution applies conservative local admission estimates and
  smooths arrivals. Response headers show the service's current limits.
- Cloud orchestration bounds active Batch jobs and estimated enqueued tokens.
- Configured prices and token estimates are planning controls, not final bills.
- In-flight cloud work can continue after a client deadline or monitoring stop.
- Budget alerts are not an instantaneous billing cutoff.

## Authentication

Runtime credentials use managed identity, with roles scoped to the new resources.
Batch file/job writes require the selected built-in OpenAI Contributor role;
review a finer-grained custom role for production. The demo does not grant
subscription-level Owner/Contributor to its runtime.

Local development uses Azure CLI authentication. The `default` credential mode
is opt-in; do not rely on an accidental cached identity.

## Retention and deletion

Input/output blobs and service-uploaded Batch files can remain after a run.
Define and implement a retention policy for your organization before real data.
Download required results before deleting anything. The starter does not
automatically delete user data or environments.

Git ignores local outputs, state databases, `.azure`, `.env`, build outputs,
and live reports. This does not replace a pre-publication content/secret review.

## GitHub automation

Offline CI runs without Azure credentials. The separate live workflow requires
`workflow_dispatch`, charge approval, `ENABLE_LIVE_TESTS=true`, a protected
`azure-demo` environment, and explicitly configured federated identity/secrets.
It targets an existing deployment and does not provision infrastructure from
untrusted pull requests. No federation or secrets are configured automatically.

The scheduler-native live workflow needs the protected environment values
`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
`AZURE_OPENAI_ENDPOINT`, `AZURE_STORAGE_ACCOUNT`, `DTS_ENDPOINT`, and
`TASKHUB_NAME`. Its federated identity needs appropriate scheduler data access;
the deployed runtime identity performs the Storage and model operations.

## Partition sizing

The default allows up to 10,000 records in a file, still bounded by file bytes
and estimated enqueued tokens. Do not split a small 1,700-record dataset into
many tiny Batch jobs solely to imitate client-side partitions. Each job has
service-managed queueing overhead. Use a smaller `records_per_batch` deliberately
when demonstrating partition-level recovery or when other limits require it.

Processing success means complete reconciliation and schema-valid output.
It does not mean every AI classification is semantically correct. Inspect the
separate label-accuracy metric and evaluate your own task's quality.
