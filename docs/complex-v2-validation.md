# V2 measured results: execution succeeded, quality gates failed

The approved 13-case `complex-v2` comparison completed on **2026-11-09 UTC**.
All **39 outputs** across sequential, five-worker parallel, and Azure-hosted
Batch were schema-valid and reconciled, with **zero observed 429s**.
However, **all three semantic acceptance gates returned `needs_review`**.
The load command correctly exited `1`; successful API processing was not
misreported as correct analysis.

The [sanitized JSON evidence](complex-v2-results.json) preserves the full field
accuracy, rule compliance, scenario breakdown, timing, usage, and gate outcomes.
No account identifiers, endpoints, remote job IDs, or private input records are
published.

## What actually ran

- The real [Markdown prompts](../prompts) contained 425 synthetic routes,
  850 examples, and multi-field evidence/confidence rules. This was not the
  original filler-only token stress test.
- Thirteen seeded conversations covered all thirteen scenario families, with
  expected answers excluded from inference requests.
- Model: GPT-5.4 mini `2026-03-17`; low reasoning effort; 2,048 output-token
  ceiling. South Central US resources used US Data Zone inference.
- Server allocation was unchanged: **600,000 TPM / 600 RPM** online, as
  confirmed by response headers. Client pacing was **480,000 TPM / 120 RPM**.
  The 150K service allocation was **not** retested in this experiment.
- Local estimates were 32,336-32,621 input tokens per request. Online API
  usage was 30,463-30,700; tokenization/schema accounting differs.
  Each complete stage reported the same **397,487 total input tokens**.
- The cloud stage used the deployed version-3.0 Durable workflow and one real
  service-managed Batch job. Its prompt/schema bundle hash matched the client.

## Processing and timing

| Mode | Schema-valid / expected | Full observed seconds | Observed 429s | Primary route correct |
|---|---:|---:|---:|---:|
| Sequential online | 13/13 | 44.72 | 0 | 10/13 (76.9%) |
| Parallel online, 5 workers | 13/13 | 14.38 | 0 | 11/13 (84.6%) |
| Azure-hosted Batch | 13/13 | 516.43 | 0 | 10/13 (76.9%) |

Batch service creation-to-completion was **464 seconds**. Application-only
elapsed was **493.65 seconds**; full orchestration elapsed was **516.43 seconds**,
including bootstrap. None of those values is per-record inference latency.
The entire load matrix took **732.87 seconds**, including cooldowns and observation.

Parallel processing was faster in this small matched run. Batch had the
expected asynchronous queue/turnaround overhead. These single observations do
not establish statistical significance, sustained capacity, a speedup SLA, or
a zero-429 guarantee.

## Semantic and rule compliance

| Check | Sequential | Parallel | Cloud Batch |
|---|---:|---:|---:|
| Primary route accuracy | 76.9% | 84.6% | 76.9% |
| Confidence within required synthetic band | 84.6% | 53.8% | 76.9% |
| Immutable archived confidence preserved | 100% | 92.3% | 100% |
| Archived confidence never lowered | 100% | 84.6% | 100% |
| Identifier-canary suppression | 100% | 100% | 100% |
| All gold-field and rule checks passed | 1/13 | 1/13 | 1/13 |
| Overall acceptance gate | needs_review | needs_review | needs_review |

The default gate requires at least 80% primary-route accuracy and 100%
confidence-rubric compliance, in addition to complete processing/evaluation,
immutable-score preservation, and canary suppression. Parallel met the route
threshold but failed confidence requirements. No thresholds or expected
answers were changed after seeing the results.

The single fully passing case in each mode was the insufficient-source case.
This strict all-fields metric is not a general model-quality score: one wrong
field makes the whole record fail, and the task explicitly requires exact
cause-sentence extraction and confidence behavior. Newly generated free-form
confidence explanations are checked for presence and canary suppression,
not independently judged for semantic truth.

Observed failure patterns included promoting a retracted goal to primary,
adding irrelevant secondary routes, confusing an intent sentence with its
cause, inventing information where Unknown was required, and violating
confidence-preservation rules. For example, the sequential corrected-goal case
selected the retracted Dispute route instead of the corrected Fault route.
Differences between modes are not evidence that concurrency caused a semantic
error; this was one small, nondeterministic run per mode.

**The important finding is that removing throttling did not solve analytical
correctness.** This workload is useful as a regression/load test precisely
because it exposes those failures. It is not a production-ready classification
prompt or evidence of equivalent performance on real customer data.

## Usage and cost estimates

| Mode | Reported cached input tokens | Reported output tokens | Reported reasoning tokens | Estimated model usage USD |
|---|---:|---:|---:|---:|
| Sequential | 359,424 | 5,644 | 4,240 | 0.0890 |
| Parallel | 270,080 | 5,940 | 4,504 | 0.1568 |
| Cloud Batch | 323,840 | 5,125 | 3,700 | 0.0564 |

Total configured-rate usage estimate: **USD 0.3022**, compared with the
conservative planned ceiling of **USD 3.1205** including online retries.
Neither figure is a complete Azure invoice; infrastructure is excluded.
Cache differences affect cost and latency, so do not attribute every difference
solely to parallelism or deployment type.

## Validation and reproducibility

The implementation passed **91 Python tests**, **three real browser tests**,
lint/type checks, source/wheel checks, and real azd packaging. The Windows
packaging path includes the same Markdown assets as POSIX, verified by hashes
and a native-wrapper regression test. The installed wheel was also exercised
independently of the source checkout.

[Cross-platform CI](https://github.com/patmeh1/azure-llm-batch-starter/actions/runs/42660481597)
passed Windows and Ubuntu with Python 3.11/3.12, plus browser and infrastructure
jobs. Python and npm dependency audits reported no known vulnerabilities at
validation time, not a permanent guarantee.

Use the [complex-prompt tutorial](complex-prompts.md) to reproduce the preview
and explicitly authorize a new paid matrix. The 26-record concurrency scenario
is provided but was **not** executed in this validation. No 1,700-record complex
run, new model, capacity increase, or permission expansion was performed.

The deployed demo remains available. The completed cloud run released its
Batch admission reservation; failed semantic checks were retained in its
operational report rather than silently retried.
