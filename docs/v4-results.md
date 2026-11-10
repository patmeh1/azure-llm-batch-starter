# V4 live pilot: one deployment versus three at matched quota

**The three-deployment pool completed more records and recorded fewer 429
attempts, but took longer. It did not eliminate throttling or achieve
complete processing.** This is a mixed result, not a threefold scaling win.

Measured on **2026-11-10** using actual GPT-5 mini `2025-08-07`,
DataZoneStandard in the US Data Zone. Regional resources were in South
Central US. Both arms used the unchanged complex-v2 Markdown/schema,
65 identical synthetic records, five workers, low reasoning, a 2,048-token
output ceiling, and the same four-total-attempt routing/retry policy.

The Azure Durable workflow reached **Completed**, with application status
**`needs_attention`** and acceptance **`needs_review`**. All 130 expected
record evaluations have terminal outcomes: **100 schema-valid, 30 failed,
zero pending**. No ambiguous online result was reported; the normal
completion path released the router lease. No replacement paid run was
started to improve these numbers.

## Matched comparison

| Metric | Single deployment | Three-deployment pool |
|---|---:|---:|
| Per-target allocation | 150K TPM / 150 RPM | 50K TPM / 50 RPM each |
| Active aggregate allocation | 150K TPM / 150 RPM | 150K TPM / 150 RPM |
| Expected records | 65 | 65 |
| Schema-valid records | **44** | **56** |
| Final failed records | 21 | 9 |
| Pending records | 0 | 0 |
| Processing success rate | 67.7% | 86.2% |
| Full arm elapsed | **464.36 s / 7m 44s** | **616.21 s / 10m 16s** |
| Validated records per minute | 5.69 | 5.45 |
| HTTP attempts | 180 | 123 |
| HTTP 429 attempts | **136** | **67** |
| All-record mean elapsed, including failures | 32.87 s | 41.73 s |
| Successful-record mean elapsed | 27.49 s | 38.71 s |
| HTTP 200 p50 | 6.29 s | 6.42 s |
| HTTP 200 p95 | 9.06 s | 8.09 s |
| Summed router wait | 1,332.34 s | 2,146.59 s |
| Reported token-usage estimate | $0.1531 | $0.1732 |
| Acceptance gate | needs_review | needs_review |

The pool produced **12 more schema-valid records** and **50.7% fewer
429 attempts**, with **32.7% longer arm duration**. Its observed
validated-record rate was slightly lower, not higher. The failure counts
prevent a claim about time to successfully process all 65 records.

All final record failures were `RateLimitError` after the bounded attempt
policy. A 429 attempt is not a failed record: some records recovered on a
later attempt. Across the two arms, 100 successful HTTP responses plus
203 HTTP 429 responses reconcile to 303 HTTP attempts.

The entire workflow took **1,149.31 seconds / 19m 9s**, including the
65-second pause between arms and orchestration/bootstrap overhead. This
is a 130-evaluation pilot, not a timing result for the 1,700-record
scenario. Application artifacts span 18:05:03-18:24:09 UTC.

## Per-deployment observations

| Alias | Allocation TPM / RPM | Attempts | HTTP 200 | HTTP 429 | Observed limit headers |
|---|---:|---:|---:|---:|---|
| control | 150,000 / 150 | 180 | 44 | 136 | 150,000 / 150 |
| pool-1 | 50,000 / 50 | 42 | 18 | 24 | 50,000 / 50 |
| pool-2 | 50,000 / 50 | 36 | 19 | 17 | 50,000 / 50 |
| pool-3 | 50,000 / 50 | 45 | 19 | 26 | 50,000 / 50 |

The three pool targets received traffic; none was the control endpoint.
Where rate-limit headers were returned, observed limits matched the
configured allocations. All 203 429 responses matched the application's
**rate-limit wording** classifier. This is not a direct measurement of
backend scale-up state, nor proof that transient capacity played no role.

The region/model/type quota was sufficient to create all four deployments.
The deployment used existing allocation, not an approved quota increase.
No additional subscription, cross-region deployment, Global fallback,
provisioned throughput, or Azure Batch job was used.

## The latency distinction matters

Successful HTTP p50 stayed around 6.3-6.4 seconds, while successful-record
means were 27.5-38.7 seconds. The latter include retries, router waiting,
and orchestration/storage work. They must not be presented as model-only
latency. Summed router wait spans concurrent workers and can exceed wall
time; it is not an extra duration to add to the arm total.

The router used service retry guidance, shared cooldowns for individual
targets, and round robin among eligible targets. SDK retries were disabled.
There was **no proactive TPM pacer** in this observation profile. Therefore
this is not evidence that properly paced production traffic would have the
same failure rate, and it is not a recommendation to run unpaced.

## Semantic correctness remains a separate problem

| Quality measure | Single | Pool |
|---|---:|---:|
| Responses evaluated | 44 / 65 | 56 / 65 |
| Primary route correct among evaluated | 41 / 44, 93.2% | 52 / 56, 92.9% |
| Confidence field correct among evaluated | 39 / 44, 88.6% | 52 / 56, 92.9% |
| Every gold-field and rule check passed | **3 / 65** | **5 / 65** |
| Records without an evaluable response | 21 | 9 |

Both acceptance gates failed. The primary-route threshold passed among
evaluated outputs, but confidence compliance and complete processing/
evaluation did not. Cause extraction, secondary topics, improvement
selection, and other exact-field checks also exposed substantial errors.
No prompt, expected answer, or threshold was changed to hide those findings.

The evaluated subsets differ because of throttling failures. Do not treat
these percentages as a controlled model-quality ranking against v2 or v3.
Free-form newly generated confidence explanations are checked for presence
and identifier suppression, not independently judged for semantic truth.
See the full per-field/scenario breakdown in the JSON evidence.

## Usage and cost boundaries

| API-reported usage | Single | Pool |
|---|---:|---:|
| Input tokens | 1,345,364 | 1,712,359 |
| Cached input tokens, subset of input | 1,147,904 | 1,548,928 |
| Output tokens | 30,560 | 38,934 |
| Reasoning tokens, subset of output | 23,936 | 30,336 |

Combined reported token-usage estimate: **$0.3263**. This applies configured
retail rates to reported usage; it is **not an invoice** and excludes
infrastructure, missing usage, taxes, and billing adjustments. Usage was
not available for every expected record because some requests failed.
Do not treat unreported usage as a measured zero.

The pre-run, uncached-input/output-ceiling, all-attempt estimate was
**$6.9867**, below the **$10** pilot guard. The difference is not a billing
error: actual attempts, returned output sizes, and cache usage differ from
the conservative reservation model. Quota and application estimates are
not subscription spending caps.

## What this supports

1. **Keep the routing result nuanced.** In this pilot the pool improved
   completion and reduced throttled attempts, but did not improve the
   observed validated-record rate or deliver complete results.
2. **Add admission control before promising scale.** Respect per-target
   request/token headroom, ramp traffic gradually, and coordinate retries.
   The reusable paced-online path in v1 is a different test configuration;
   this v4 pilot did not test a token-paced multi-target router.
3. **Treat accuracy independently.** Preserve withheld-gold evaluation when
   simplifying the prompt, retrieving a smaller taxonomy subset, or
   evaluating another model. More deployment names do not fix wrong fields.
4. **Do not infer hidden capacity state.** Capture headers, timestamps,
   request IDs privately, and error indications for support investigation.
   This pilot cannot establish backend ramp-up behavior from 429s alone.

## Evidence, outputs, and reproduction

- [Machine-readable aggregate results](v4-results.json).
- [Six actual synthetic record outcomes](v4-output-samples.json), the
  first three IDs from each arm, including one failed record. They are
  not cherry-picked successful answers or the full output dataset.
- [Step-by-step deployment and operations guide](v4-multi-deployment.md).
- [65-record pilot configuration](../scenarios/v4-matched-quota-pilot.json).
- [1,700-record configuration, not executed](../scenarios/v4-matched-quota-1700.json).
- [The unchanged runtime Markdown prompts](../prompts).
- [Public v4 page](https://patmeh1.github.io/azure-llm-batch-starter/v4.html)
  and [four-version comparison](https://patmeh1.github.io/azure-llm-batch-starter/comparison.html).

Full records and attempt artifacts remain in private Azure Storage.
Public exports exclude account endpoints, tenant/subscription identities,
private paths, raw error messages, and the private source discussion.

The full 1,700-record scenario was previewed only: its conservative
four-attempt estimate is **$182.7299** under a $200 model guard. No full-size
v4 run occurred, and the interrupted v3 experiment remains incomplete.
