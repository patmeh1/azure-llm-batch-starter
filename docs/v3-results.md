# V3 GPT-5 mini results

**The full 1,700-record-per-mode experiment is in progress. Final results are
not available yet.** The sequential stage runs first; five concurrent groups
of 340 run afterward against the same deployment.

The [machine-readable snapshot](v3-results.json) includes its observation
timestamp and an explicit `results_final` flag. Partial metrics are not a
completed throughput benchmark or proof of a zero-429 outcome.

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
