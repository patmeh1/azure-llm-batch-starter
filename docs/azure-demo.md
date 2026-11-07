# Deploy and run the Azure demo

## 1. Prerequisites

- Python 3.11+, preferably 3.12; Azure CLI; Azure Developer CLI (`azd`).
- An Azure subscription with permission to deploy the selected services and
  assign resource-scoped data roles. Contributor alone cannot assign roles.
- GPT-5.4 mini Data Zone online and Batch quota in your selected region.
- Permission to use US Data Zone processing for the default South Central US
  deployment. Resource location and inference geography are different concepts.

Download the source ZIP and extract it, or clone the public repository. Run the
commands from its root. The defaults do not require Docker.

```powershell
python -m pip install -e ".[dev,cloud]"
az login
az account list --output table
az account set --subscription "YOUR_SUBSCRIPTION_ID"
$subscription = az account show --query id --output tsv
python scripts\preflight.py --subscription $subscription --region southcentralus
```

Preflight is read-only. It checks the **exact model version and deployment
types**, unallocated quota, and Functions/Scheduler availability. It never
silently selects another model or region. Model catalog availability is not a
promise of immediate deployable capacity.

If needed, register the provider as a deliberate deployment prerequisite:

```powershell
az provider register --namespace Microsoft.DurableTask --wait
```

## 2. Configure a new environment

Use a unique environment name to avoid modifying someone else's resource group.
The default group name is `rg-<environment-name>`.

```powershell
azd env new my-batch-demo --subscription $subscription --location southcentralus --no-prompt
azd env set VNET_ENABLED false
azd provision --preview --no-prompt
```

Authenticate `azd` to the same tenant/subscription. If necessary use `azd auth
login`; alternatively configure its documented Azure CLI authentication option.
Do not continue when `az` and `azd` select different accounts.

Review the preview and costs. The template creates an AI Services account/project,
two model deployments, Functions Flex, a user-assigned managed identity,
Durable Task Scheduler Consumption, a task hub, private Blob/Queue/Table data,
and logging. An enforced Network Security Perimeter protects Storage. All
regional resources share one location parameter.

The default synthetic demo is launched through the authenticated Durable Task
Scheduler client. It generates records **inside Azure**, so your laptop does
not need direct Storage access. The perimeter allows subscription-based
managed-identity traffic for the Functions runtime, while all callers still
need data-plane roles.

For direct custom-file upload or artifact download, arrange approved Storage
connectivity. If an external IP rule is appropriate, ask your network
administrator for the actual address used to reach Storage, then configure:

```powershell
azd env set ALLOWED_CLIENT_PREFIX "YOUR_APPROVED_PUBLIC_IPV4/32"
```

Do not use `0.0.0.0/0`, an internal service endpoint address, or a policy exclusion.
Some routed clients present no public source address to the perimeter; an IP rule
will not make that path work. Use an approved network-connected client for raw
artifacts rather than weakening the perimeter.

The perimeter uses `SecuredByPerimeter` and an **Enforced** association, not
policy-exclusion tags or unrestricted network access. `VNET_ENABLED` is an
advanced Storage private-endpoint option, **not** a one-switch complete network
isolation solution. Leave it false for the perimeter-based synthetic demo.

## 3. Deploy

```powershell
azd up --no-prompt
```

The post-provision hook writes `.azure\demo-config.json`. It contains your
endpoints/settings, not API keys, and must remain ignored by Git.

If data access returns 403 immediately afterward, allow time for the new role
assignments to propagate. Do not enable shared keys or weaken authentication.
See [troubleshooting](troubleshooting.md).

## 4. Start a real ten-record job

```powershell
llm-batch azure-demo --count 10 --config .azure\demo-config.json
```

Review the inference estimate and confirm. For an already approved automated
test, append `--yes`. Save the returned `run_id`.

The CLI starts `synthetic_workflow` through Microsoft's official authenticated
Durable Task SDK. An activity generates and stores the synthetic input inside
Azure. Durable Functions prepares JSONL, submits real Batch jobs, waits using
durable timers, and reconciles results. Closing the terminal does not terminate
the cloud workflow.

```powershell
llm-batch azure-status YOUR_RUN_ID --config .azure\demo-config.json --wait --timeout 900
```

Exit code `0` means all expected records succeeded; `1` means attention/failure;
`2` means monitoring ended while the job remains pending. A monitoring timeout
does **not** cancel inference. Re-run the status command later.

The operational summary is downloaded under `outputs\azure\YOUR_RUN_ID`.
Full record artifacts remain in private Storage; retrieve them from an approved
Storage-connected client. No raw records are tunneled through scheduler history.
Compare the
input count with succeeded, failed, and pending counts; do not infer success
from a workflow simply reaching its terminal state.

## 5. Open both dashboards

```powershell
llm-batch dashboard --config .azure\demo-config.json
```

Open `http://127.0.0.1:8501` and select **Azure scheduler**. Inspect the run and its batch
partitions, duration, failures, retry history, and output counts.

For framework-level diagnostics, open the deployed Durable Task Scheduler
resource in Azure Portal, select its task hub, and open the dashboard link.
This existing Microsoft dashboard shows orchestrations, activities, and timers.
Its `Completed` runtime status means orchestration ended, not necessarily that
every inference record succeeded; check the returned application status.

## 6. Run a larger workload

Start with 100 synthetic records. Review the cost and quota budget before 1,700:

```powershell
llm-batch azure-demo --count 100 --config .azure\demo-config.json
llm-batch azure-demo --count 1700 --config .azure\demo-config.json
```

For your own data, provide JSONL records with unique `id` and `text` fields:

```powershell
llm-batch azure-demo --input outputs\my-records.jsonl --transport storage --config .azure\demo-config.json
llm-batch azure-status YOUR_RUN_ID --transport storage --config .azure\demo-config.json --wait
```

This Storage transport requires approved direct network access and uses the
queue starter. Do not put real customer data in Git. Consult your own privacy, retention, and
residency requirements before using non-synthetic inputs.

## 7. Cancel and reconcile

```powershell
llm-batch azure-cancel YOUR_RUN_ID --config .azure\demo-config.json
llm-batch azure-status YOUR_RUN_ID --config .azure\demo-config.json --wait
```

Cancellation is asynchronous and completed work can still be billable.
Terminating a Durable instance in its dashboard does not itself cancel the
remote model job. Preserve job IDs and reconcile outstanding work.

For confirmed transient per-record failures after reconciliation:

```powershell
llm-batch azure-retry-failed YOUR_RUN_ID --config .azure\demo-config.json
```

The new run retains parent lineage. Permanent refusals, schema problems, and
ambiguous outcomes are not blindly resubmitted.

## 8. Keep or remove the environment

Keeping idle resources can still incur storage and logging charges. Before
cleanup, cancel/reconcile outstanding jobs and download required outputs.

**Destructive, only when you choose to remove this entire environment:**

```powershell
azd down
```

Read the deletion confirmation. Never run cleanup against a shared environment.

## Linux and macOS

The same CLI works. Use shell paths such as `.azure/demo-config.json` and:

```bash
subscription=$(az account show --query id --output tsv)
python scripts/preflight.py --subscription "$subscription" --region southcentralus
azd env new my-batch-demo --subscription "$subscription" --location southcentralus --no-prompt
azd up --no-prompt
bash scripts/demo.sh --count 10
```
