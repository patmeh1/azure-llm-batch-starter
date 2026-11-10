# V3 GPT-5 mini results

**The full 1,700-record-per-mode experiment is interrupted and incomplete.
Final comparison results are not available.** The last recorded request
activity is **2026-11-09 22:05:28 UTC**. A subsequent process check found no
matching experiment runner; the interruption cause is not established.

The preserved checkpoint contains **654 schema-valid sequential records**,
zero final record failures, **1,046 pending records**, and **641 HTTP 429
attempts**. The five concurrent groups of 340 have not started. An in-flight
request without a saved result is not counted as successful.

No new paid run was started during the website update. The original checkpoint
and private artifacts are preserved for investigation and explicit recovery.

The [machine-readable snapshot](v3-results.json) includes its observation
timestamp and an explicit `results_final` flag. Partial metrics are not a
completed throughput benchmark or proof of a zero-429 outcome.
Its `observed_at` means the snapshot was inspected, not that inference made
progress at that time. The snapshot also records `last_activity_at` and the
checkpoint's original state separately from the observed interrupted state.

## Verified setup

- GPT-5 mini `2025-08-07`, US DataZoneStandard, South Central US resource.
- 150,000 TPM and 150 RPM confirmed in deployment metadata and real response
  headers.
- Exactly the v2 synthetic Markdown/schema bundle:
  `469532c76922192658dd12a81cd842a8b2328d529db6acbb4c433385e30cb4fe`.
- 1,700 identical seeded records for each mode; five fixed groups of 340 in
  `client-batch`.
- Client-side grouped online calls, **not** Azure asynchronous Batch.
- Low reasoning effort, 2,048-token output ceiling, at most three attempts,
  ten hours per mode, and a USD 150 conservative model-usage scenario guard.
- Existing v2 model deployments remain unchanged.

The application passed 108 Python tests and three browser tests locally,
including grouping, legacy fingerprint compatibility, ambiguous-result resume,
attempt/deadline preservation, and online-only preflight semantics.

The model and processing pattern are controlled; the quota/retry/timeouts are
explicit test assumptions. No claim is made that this exactly reproduces an
unknown production VM, network path, or request configuration.

Follow the [v3 guide](v3-gpt5-mini.md) to inspect checkpoints or reproduce the
scenario. Final output CSV/JSONL files are generated only from terminal,
reconciled live results, not estimates or mock outputs.

The [partial output sample](v3-partial-output-sample.json) contains the first
three completed sequential records, including their quality checks. It is
explicitly a small progress sample, not the full comparison or a quality claim.
