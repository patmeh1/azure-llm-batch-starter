# Azure LLM Batch Starter

Process independent records through Azure OpenAI, track every result, and see
where time is spent. Includes **actual Batch API processing**, sequential and
bounded-parallel comparisons, **Azure Durable Functions orchestration**, and a
read-only dashboard for durations, failures, retries, token rates, and tests.

All examples are synthetic. No Azure credentials or customer content are included.

## Choose your starting point

| Goal | Start here |
|---|---|
| Explore the dashboard without Azure or charges | Offline demo below |
| Deploy a runnable Azure demo into your own subscription | [Azure tutorial](docs/azure-demo.md) |
| Process a JSONL file and compare modes | [Local tutorial](docs/tutorial.md) |
| Investigate token limits, retries, and 429s | [Load testing](docs/load-testing.md) |
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
