# Live Azure validation

These are **measured synthetic experiments**, not simulated dashboard rows,
production benchmarks, or throughput guarantees. Measurements were collected
on 2026-11-07 UTC. The [machine-readable evidence](live-results.json) contains
allowlisted aggregate metrics, without account identifiers, endpoints, record
bodies, or remote job IDs.

## Tested configuration

GPT-5.4 mini `2026-03-17`, South Central US resources, `DataZoneStandard` online
and `DataZoneBatch` asynchronous deployments. Inference uses the **US Data
Zone**, not guaranteed South Central US-only processing. Inputs were generated
support tickets, seed 42, with structured output, low reasoning effort, and a
1,024-token generation ceiling.

The cloud path used real Durable Functions workers, Durable Task Scheduler,
managed identities, enforced Storage perimeter rules, and the actual Batch
API. It did not substitute a local runner for cloud execution.

## Compact 100-record comparison

| Mode | Client RPM cap | Validated records | Full observed seconds | Observed 429s |
|---|---:|---:|---:|---:|
| Sequential online | 60 | 100/100 | 104.01 | 0 |
| Parallel online, 5 workers | 60 | 100/100 | 100.26 | 0 |
| Sequential online | 120 | 100/100 | 90.34 | 0 |
| Parallel online, 5 workers | 120 | 100/100 | 50.69 | 0 |
| Cloud Batch | Separate Batch quota | 100/100 | 506.84 | 0 |

The matched inputs used 14,862 API-reported input tokens in each run. With a
shared 60-RPM client cap, additional workers did not materially improve
throughput. At 120 RPM, parallel processing finished in about 56% of the
sequential run's time. This is one observation, not a promised speedup.

Batch was **slower for this small dataset** because asynchronous turnaround
dominated. Its reported-usage token-cost estimate was about USD 0.0224,
versus USD 0.0443 for parallel online processing. These estimates exclude
infrastructure, missing usage, taxes, and billing adjustments.

Processing success means valid, reconciled outputs. Sequential category-label
accuracy was 99%; parallel and Batch accuracy was 100% in these particular
100-record runs. Schema validity does not establish semantic correctness.

## Capacity and large-input experiments

The initial large-input profile produced approximately 72,090 locally
estimated input tokens per request. At the observed **150,000 TPM / 150 RPM**
service allocation, six-record bursts produced:

| Concurrency/profile | Succeeded | Failed with 429 |
|---|---:|---:|
| 1, stable prefix | 3 | 3 |
| 2, stable prefix | 2 | 4 |
| 5, stable prefix | 3 | 3 |
| 5, varied early prefix | 2 | 4 |

The same six-record stable-prefix workload at five workers completed **6/6
with zero observed 429s** after the dedicated online deployment's allocation
was increased to the response-confirmed **600,000 TPM / 600 RPM**. A separate
selective retry recovered only the three previously failed records, without
reprocessing their successful peers.

Cache behavior differed: the matched five-worker runs reported 215,808 cached
tokens at the lower allocation and 137,216 at the higher allocation. Do not
attribute their latency or token-cost differences solely to capacity. The
strong observation is successful admission of all six requests, not a
sustained records/minute claim.

### Calibrated doubled-token profile

An early generator repeated encoded token units; re-encoding the concatenated
text changed token boundaries and undershot its requested target. That issue
was fixed by tokenizing the complete repeated text and calibrating against
the assembled request. Regression tests verify the actual payload size.

The final profile targeted **74,572 estimated tokens**, twice the illustrative
37,286-token baseline. Across six records, actual local estimates were
**74,568-74,572** and online API usage was **74,534-74,538** per response.
The small difference reflects request-estimation versus service accounting;
the baseline is not measured production API usage.

| Final calibrated test | Validated records | Full observed seconds | 429s | Reported input tokens |
|---|---:|---:|---:|---:|
| Online, 5 workers, 600k TPM allocation | 6/6 | 8.53 | 0 | 447,214 |
| Azure-hosted Batch | 6/6 | 493.40 | 0 | 447,214 |

Both runs had 100% category-label accuracy. Online reported 367,872 cached
tokens; Batch reported 221,952. Repeated synthetic reference material is not
a substitute for evaluating a real workload's reasoning requirements.

These were bounded bursts with one online attempt per record, not sustained
capacity tests. Zero observed 429s here is **not a zero-429 guarantee**.

## 1,700-record scale and recovery test

All **1,700/1,700 records** were reconciled from **one actual Batch job**, with
zero failed or missing outputs and zero observed 429s. Reported usage was
252,648 input and 111,403 output tokens. Category-label accuracy was
**99.53%**: eight labels were incorrect despite schema-valid output.

| Measurement | Observed duration |
|---|---:|
| Service-reported Batch creation to completion | 793 seconds / 13 min 13 sec |
| Full application elapsed, including recovery delay | 1,713.68 seconds / 28 min 34 sec |
| Recovery orchestration alone | 28.77 seconds |

The short recovery interval is **not** the time to infer 1,700 records.

During development, a rolling deployment left an older worker briefly active.
It started an unversioned orchestration that subsequently replayed on newer
control flow and failed determinism checks, while its already-submitted Batch
job continued. Rewind did not repair the incompatible recorded events.

The implementation now uses explicit worker/client/sub-orchestration versions
and an idempotent recovery path. Recovery used fresh execution histories with
the **original saved manifest, partition, reservation key, and remote Batch
ID**. The unchanged remote job identity was verified; no replacement inference
job was needed. Original failed histories remain diagnostic evidence.

See [versioning and operator recovery](orchestration.md). This run demonstrates
scale correctness and recovery, **not an uninterrupted latency benchmark**.

## Cancellation and resume

- An actual ten-record cloud run was cancelled before service submission.
  Its job was `cancelled_before_submission`, with no remote Batch ID, zero
  successes, and ten unfinished records. The application returned
  `needs_attention`, not a misleading full-success result.
- An earlier local ten-record Batch job was resumed using its original
  submission and reconciled 10/10. Service duration was 2,737 seconds.
  Observed application wall time was 5,715.79 seconds because the observer
  had been paused. Paused time was not removed to make throughput look better.
- A separate cloud ten-record smoke run reconciled 10/10. Its 90% label
  accuracy is retained in the evidence rather than hidden.

`pending` in result summaries means records without reconciled outputs; after
cancellation it does not mean they are still queued for inference.

## Automated validation

- **53 Python tests** cover processing, reconciliation, pacing, fault handling,
  token calibration, versioning, cancellation, recovery, timing, and dashboard
  behavior.
- **Two real browser tests** cover the Streamlit UI and exports.
- [GitHub CI](https://github.com/patmeh1/azure-llm-batch-starter/actions/workflows/ci.yml)
  passed on Windows and Ubuntu with Python 3.11 and 3.12, plus browser and
  infrastructure jobs.
- Wheel/source builds, Bicep compilation, linting, and type checking passed.
  The resolved Python dependency audit and npm audit reported no known
  vulnerabilities at validation time; this is not a permanent security claim.
- Release 0.1.1 additionally enforces LF shell scripts in Windows checkouts
  and inspects built archive bytes. This fixes the Windows-built source
  archive's shell-script line endings from 0.1.0; the Python processing code
  and measured inference results are unchanged.
- Docker/emulator tests were not run. Real Azure validation is listed
  separately above.

## Reproduce safely

Follow the [Azure tutorial](azure-demo.md), then the
[bounded load-testing guide](load-testing.md). Use your own approved quota,
budget, identity, network route, and data-residency decision. Record failures
and actual usage as well as successful timings.

Direct client Storage transfers require approved connectivity. The tested
scheduler launcher generates data inside Azure and returns operational
metadata only; it does not bypass Storage isolation or expose raw documents.
The custom-file/Storage transport was not end-to-end validated from this
client because that routed network path is restricted.
