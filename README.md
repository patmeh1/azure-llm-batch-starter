# Azure LLM Batch Starter

Process independent records through Azure OpenAI, track every result, and see
where time is spent. Includes **actual Batch API processing**, sequential and
bounded-parallel comparisons, **Azure Durable Functions orchestration**, and a
read-only dashboard for durations, failures, retries, token rates, and tests.

All examples are synthetic. No Azure credentials or customer content are included.

**Customer-facing website:** [Overview](https://patmeh1.github.io/azure-llm-batch-starter/)
and [side-by-side comparison](https://patmeh1.github.io/azure-llm-batch-starter/comparison.html).
Separate pages explain v1/v2 **GPT-5.4 mini** versus v3 **GPT-5 mini**, the exact
configuration changes, measured outcomes, limitations, prompts, and downloads.
The site does not connect to Azure or trigger inference.

## Choose your starting point

| Goal | Start here |
|---|---|
| Explore the dashboard without Azure or charges | Offline demo below |
| Deploy a runnable Azure demo into your own subscription | [Azure tutorial](docs/azure-demo.md) |
| Process a JSONL file and compare modes | [Local tutorial](docs/tutorial.md) |
| Investigate token limits, retries, and 429s | [Load testing](docs/load-testing.md) |
| Run complex synthetic prompts stored in Markdown | [V2 complex workload](docs/complex-prompts.md) |
| Compare GPT-5 mini sequential calls with five groups of 340 | [V3 reproduction guide](docs/v3-gpt5-mini.md) |
| Inspect the full v3 experiment's current evidence | [V3 results/status](docs/v3-results.md) |
| Maintain or republish the public HTML site | [Customer site guide](docs/customer-site.md) |
| Review v2 timing and failed semantic acceptance gates | [V2 measured results](docs/complex-v2-validation.md) |
| Understand the reusable Microsoft framework | [Orchestration](docs/orchestration.md) |
| Review topology, authentication, and data flow | [Architecture](docs/architecture.md) |
| Understand what each measurement actually means | [Metrics](docs/metrics.md) |
| Run tests or inspect validation evidence | [Testing](docs/testing.md) |
| See measured Azure results, including the 1,700-record recovery test | [Live validation](docs/live-validation.md) |

Clone [the public repository](https://github.com/patmeh1/azure-llm-batch-starter)
or download a source archive from its Releases page:

```powershell
git clone https://github.com/patmeh1/azure-llm-batch-starter.git
Set-Location azure-llm-batch-starter
```

## Offline demo: no Azure sign-in required

Use Python 3.11+ (3.12 is the tested Functions runtime). From the repository root:

```powershell
python -m pip install -e ".[dev,cloud]"
llm-batch dashboard --demo
```

Open `http://127.0.0.1:8501`. **Demo rows are labeled SIMULATED** and do not claim
to measure Azure performance. No Docker or cloud resources are needed for this path.

Run timed offline tests:

```powershell
python scripts\run-tests.py -q
```

## V2 complex-prompt workload

Version **0.2.0** adds a domain-neutral, opt-in `complex-v2` profile with
**425 hierarchical routes, 850 examples, and thirteen conversation scenarios**.
The actual instructions and taxonomy are in [`prompts`](prompts), and are
packaged with both the Python application and Azure worker.

```powershell
llm-batch generate --profile complex-v2 --count 13 --output outputs\complex-v2.jsonl
llm-batch load-test --scenario scenarios\complex-v2-smoke.json --config .azure\demo-config.json
```

The second command previews the cost/token plan without inference. Add `--yes`
only after reviewing it to compare sequential, paced parallel, and actual cloud
Batch on the same complex cases. Reports and the dashboard distinguish
**schema success from semantic accuracy and rule compliance**.
See the [complete v2 tutorial](docs/complex-prompts.md).
The [live v2 comparison](docs/complex-v2-validation.md) completed all three
processing modes without observed 429s, but failed semantic quality thresholds.
Those failures remain visible in the evidence and dashboard.

## V3 GPT-5 mini grouped-request experiment

Version **0.3.0** adds an additive US DataZoneStandard model template and a
checkpointed 1,700-record sequential versus five-by-340 `client-batch` comparison.
It reuses the v2 Markdown prompts unchanged and preserves v2 deployments.
Client-side grouping is explicitly **not the Azure Batch API**; the checked
GPT-5 mini regional catalogs did not list DataZoneBatch.

The full comparison is currently interrupted and incomplete; the
[results/status](docs/v3-results.md) preserve partial evidence, not final results.
See the [v3 guide](docs/v3-gpt5-mini.md)
for assumptions, budget/attempt/deadline guards, resume behavior, and output export.

## Real Azure demo

The [complete tutorial](docs/azure-demo.md) covers prerequisites, sign-in,
preflight, costs, deployment, execution, dashboards, and cleanup. After those
steps:

```powershell
azd up --no-prompt
llm-batch azure-demo --count 10 --config .azure\demo-config.json
llm-batch azure-status YOUR_RUN_ID --config .azure\demo-config.json --wait
llm-batch dashboard --config .azure\demo-config.json
```

The Azure workflow continues if your laptop disconnects. The custom dashboard
runs locally and can read Azure metadata; the managed Durable Task Scheduler
also provides its own authenticated cloud dashboard.

**Defaults:** all regional resources in `southcentralus`; GPT-5.4 mini
`2026-03-17`; `DataZoneStandard` and `DataZoneBatch`. Model availability and
quota must be checked in your subscription. **US Data Zone processing is not
guaranteed to stay within South Central US alone.**

## What this solves, and what it does not

- Durable job tracking, exact `custom_id` reconciliation, selective retry,
  explicit incomplete/failed states, and reproducible synthetic benchmarks.
- Shared local online pacing and cloud Batch admission control; not unlimited
  throughput and not a promise of zero 429s.
- Real Batch has separate enqueued-token quota and asynchronous turnaround.
  Five client-side request groups are not the Batch API.
- It is a reference implementation, **not a production SLA**. Review networking,
  data retention, model evaluation, and fine-grained permissions before production.
- Do not load sensitive data into a public demo or publish your `outputs`,
  `.azure`, `.env`, or raw cloud logs.

## Deployment and runtime costs

Uses Functions Flex on-demand, Durable Task Scheduler Consumption, Storage,
monitoring, and token-billed model deployments. No PTUs, Dedicated scheduler,
or always-ready Functions instances are enabled by default. Allocation of model
quota is not a throughput guarantee.

Storage is protected by an **enforced Network Security Perimeter**, allowing
managed-identity traffic from the deployment subscription and one explicitly
approved test-client address. Entra authentication is still required. Anonymous
Blob access, shared Storage keys, and OpenAI API keys are disabled. The tutorial
includes the client-address setup. The optional Storage VNet/private-endpoint
mode requires a client with network access; it does not make the entire
application private automatically.

The live GitHub workflow is opt-in and disabled unless you explicitly configure
its protected environment and enable it. Offline CI never calls Azure.

MIT licensed. See [upstream attribution](THIRD_PARTY_NOTICES.md).
