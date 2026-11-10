# V5 measured results: complete processing without observed 429s

**The quota-aware arm processed all 520 records with zero observed 429s.
Its mean successful-record processing time fell from 21.99 to 14.37 seconds
(34.6% lower), but the whole arm took 4.4% longer.** Mean successful HTTP
call duration was essentially unchanged at 6.02 seconds.

This is a reliability and record-processing-time improvement in one paired
experiment, **not a demonstrated full-job speedup or a zero-429 guarantee**.
The reactive baseline failed nine records, so its time to finish all 520
successfully is not established.

Measured on **2026-11-10**, using five actual GPT-5 mini `2025-08-07`
US DataZoneStandard deployments: one 100K and four 50K targets, totaling
**300K TPM / 300 RPM in both arms**. Both used the same 520 synthetic records,
seed 42, unchanged complex-v2 Markdown/schema, low reasoning, a 2,048-token
output ceiling, and five workers. Raw records remained in private storage.

## Main comparison

| Metric | Reactive round robin | V5 quota-aware scheduling |
|---|---:|---:|
| Distinct benchmark records reaching inference | **520** | **520** |
| Schema-valid records | **511 / 520** | **520 / 520** |
| Failed records | 9 | **0** |
| Pending records | 0 | 0 |
| HTTP attempts | 957 | 520 |
| HTTP 429 attempts | **446** | **0** |
| Full measured arm time | 3,047.08 s / 50m 47s | 3,181.19 s / 53m 01s |
| Validated records/minute | 10.06 | 9.81 |
| Average successful HTTP call | **6.02 s** | **6.02 s** |
| Average successful record processing | **21.99 s** | **14.37 s** |
| Average all-record processing | 21.91 s | 14.37 s |
| Average enqueue-to-completion | 1,485.82 s / 24.76 min | 1,576.01 s / 26.27 min |
| Reported benchmark token-usage estimate | $1.4269 | $1.2515 |
| Semantic acceptance | needs_review | needs_review |

Both 520-record arms completed their ledgers; **1,031 schema-valid outcomes
and nine confirmed failures** reconcile to 1,040 benchmark evaluations.
All nine final failures were rate-limit failures in the reactive arm.
The adaptive arm needed exactly one HTTP attempt per record.

The whole Azure workflow took **6,478.07 seconds / 1h 47m 58s**, including
bootstrap, warm-ups, pauses, and reconciliation. Application artifacts span
21:44:52-23:32:31 UTC. There were no ambiguous online outcomes; normal final
completion released the shared experiment lease.

## Average request time: three different clocks

These are actual arithmetic means with measured denominators, not total
job duration divided by record count.

| Measurement | Reactive value / count | Quota-aware value / count |
|---|---:|---:|
| Mean HTTP 200 duration | 6.0214 s / 511 | 6.0164 s / 520 |
| Mean all-HTTP duration, including errors | 3.3505 s / 957 | 6.0164 s / 520 |
| Mean successful record processing | 21.9924 s / 511 | 14.3747 s / 520 |
| Mean all-record processing | 21.9115 s / 520 | 14.3747 s / 520 |
| Mean backlog waiting before first readiness | 1,463.90 s / 520 | 1,561.64 s / 520 |
| Mean enqueue-to-completion | 1,485.82 s / 520 | 1,576.01 s / 520 |

**HTTP duration** includes the client, authentication, network, and response;
it is not pure GPU/model compute. The all-HTTP mean is artificially smaller
in the reactive arm because many 429 responses returned quickly. That is not
evidence of faster useful processing.

**Record processing** begins when a record first becomes ready at the shared
queue head. It includes admission waits and retries through response
persistence and validation, ending immediately before the final metadata
write. It excludes the backlog ahead of that record. Its successful-only
mean has a different denominator in the two arms; the all-record mean is
also shown and fell 34.4%.

**Enqueue-to-completion** starts when all records enter the measured arm.
It includes initial backlog waiting. This average increased 6.1%, consistent
with the longer whole-arm duration. The lower 14.37-second processing mean
must not be presented as the full wait experienced by every queued record.

| Latency distribution, not means | Reactive | Quota-aware |
|---|---:|---:|
| Successful HTTP p50 | 6.08 s | 5.91 s |
| Successful HTTP p95 | 8.02 s | 8.21 s |
| All-record processing p95 | 38.63 s | 27.00 s |
| Enqueue-to-completion p95 | 2,892.45 s | 3,014.15 s |

## All five deployments carried traffic

| Target | Allocated TPM | Reactive HTTP 200 / 429 | Quota-aware HTTP 200 / 429 | Reactive / quota-aware mean HTTP 200 |
|---|---:|---:|---:|---:|
| target-1 | 100,000 | 169 / 118 | 175 / 0 | 5.89 s / 5.78 s |
| target-2 | 50,000 | 85 / 83 | 85 / 0 | 6.30 s / 6.66 s |
| target-3 | 50,000 | 86 / 81 | 86 / 0 | 6.08 s / 6.33 s |
| target-4 | 50,000 | 86 / 82 | 87 / 0 | 6.05 s / 5.68 s |
| target-5 | 50,000 | 85 / 82 | 87 / 0 | 5.93 s / 5.90 s |

Returned limit headers matched 100K/100 RPM or 50K/50 RPM where reported.
The adaptive distribution roughly followed available capacity: the 100K
target handled about twice the work of each 50K target.

The admission diagnostics record 946 rejected budget-header values in the
reactive arm and zero in the adaptive arm. This counter means values were
unusable by this parser's nonnegative-budget rules; it is **not proof that
the service emitted malformed headers**, and is not an additional error or
failed-request count. Raw diagnostic headers remain private. All 446 reactive
429s matched the application's rate-limit wording classifier; this does not
reveal internal scale-up state.

## Quality did not become production-ready

| Quality measure | Reactive | Quota-aware |
|---|---:|---:|
| Evaluated responses | 511 / 520 | 520 / 520 |
| Primary route correct among evaluated | 483 / 511, 94.5% | 495 / 520, 95.2% |
| Confidence field correct among evaluated | 448 / 511, 87.7% | 456 / 520, 87.7% |
| Every gold-field and rule check passed | **45 / 520** | **49 / 520** |

Both semantic gates remained `needs_review`, including confidence compliance.
Cause extraction, secondary topics, improvement selection, and other fields
still had substantial exact-match failures. Prompt bytes, expected answers,
and acceptance thresholds were unchanged. Routing is not a remedy for those
analytical errors, and this single run does not establish a quality improvement
caused by scheduling.

## Warm-ups and costs

Each arm used five separate synthetic warm-up records, one per deployment.
Both warm-up stages completed 5/5 with zero 429s. Their observed elapsed times
were 12.17 seconds and 6.96 seconds. A 65-second pause followed each warm-up stage
and separated the arms. Warm-ups are excluded from the 520-record timing
tables and included in whole-workflow time and cost.

| API-reported usage | Reactive benchmark | Quota-aware benchmark |
|---|---:|---:|
| Input tokens | 15,624,200 | 15,899,741 |
| Cached input, subset of input | 14,687,104 | 15,760,768 |
| Output tokens | 347,879 | 354,472 |
| Reasoning tokens, subset of output | 271,232 | 276,800 |

Reported token-usage estimate including warm-ups: **$2.7174**.
Warm-ups account for **$0.0390** of that total. The pre-run conservative
four-attempt estimate was **$56.4316**, within the **$75** application guard.
These are configured-rate estimates, not an invoice, and exclude infrastructure,
unreported usage, taxes, and billing adjustments.

Cached input was 94.0% in the reactive arm and 99.1% in the adaptive arm.
The latter ran second. Therefore lower reported cost cannot be attributed
solely to the controller; cache/order effects are an explicit limitation.

## What this establishes, and what it does not

- **Observed:** 520/520 schema-valid adaptive outcomes, zero adaptive 429s,
  fewer HTTP attempts, and lower ready-to-outcome record-processing averages.
- **Not observed:** faster model-call means or faster total job completion.
  Adaptive arm time increased 4.4%; validated-record throughput was 2.5% lower.
  The baseline's nine failures prevent a complete-workload speedup comparison.
- This was one matched pair, not a randomized repeated benchmark, production
  SLA, or promise of zero future throttling.
- Both arms used the SAME 300K pool. Historical V4 used 150K per arm and 65
  records; its elapsed times are not interchangeable with these results.
- During the reactive arm, the local observer had a transient gRPC timeout.
  The Azure run continued. Monitoring reconnected to the same ID; the cloud
  controller was not redeployed or changed mid-test and no paid run was
  resubmitted. Observer reconnection now has bounded, explicit read-only retries.

## Evidence and reproduction

- [Aggregate measurements and timing denominators](v5-results.json).
- [First three actual synthetic record outcomes per arm](v5-output-samples.json).
  They are selected by ID without filtering for correctness; they are not the
  full dataset or a representative accuracy sample.
- [Step-by-step V5 deployment/run guide](v5-adaptive.md).
- [520-record scenario](../scenarios/v5-adaptive-520.json).
- [V5 webpage](https://patmeh1.github.io/azure-llm-batch-starter/v5.html).
- [Five-version comparison](https://patmeh1.github.io/azure-llm-batch-starter/comparison.html).

Full records, request IDs, private account context, and response artifacts
remain in protected Azure Storage. The public evidence is synthetic and
customer-neutral. No 1,700-record V5 test or replacement V3/V4 run was executed.
