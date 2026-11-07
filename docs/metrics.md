# Metrics and measurement semantics

| Metric | Meaning |
|---|---|
| Run elapsed seconds | UTC end minus start; includes waiting and failures |
| Orchestration elapsed seconds | Scheduler creation to terminal state, including synthetic bootstrap; shown for scheduler runs |
| End-to-end elapsed seconds | The longer of observed application and current orchestration wall time; recovery does not erase prior waiting/work |
| Completed records/minute | Successfully validated records / full wall time |
| HTTP attempt duration | Client-observed call duration, not pure GPU inference time |
| Record elapsed time | Admission waits, attempts, retries, and validation |
| Retry waiting | Actual recorded backoff sleep, not inferred from response latency |
| Stage aggregates | Time accumulated for that stage; parallel stages may overlap |
| Batch duration | Observed submission-to-terminal/collection interval |
| Service timestamps | Batch timestamps returned by Azure, when available |
| Input/output tokens | Response-reported usage; null when absent, with usage coverage recorded separately |
| Cached/reasoning tokens | Reported usage detail; missing data is not a proven zero |
| RPM | This client's observed HTTP attempts in UTC minute buckets |
| Observed TPM | Tokens reported by successful responses in those buckets |
| 429 count | Observed throttled attempts, separate from final failed records |
| Test duration | Measured suite and individual-test elapsed time |

## Do not confuse these

- Successful-record average excludes failed work; whole-job throughput must not.
- Five parallel attempts are not five independently provisioned model capacities.
- Batch queue/turnaround time is not per-record model inference latency.
- Do not divide Batch wall time by records and label it inference latency.
- Azure's enforcement windows and competing traffic are not reproduced exactly
  by client minute buckets. Pair measurements with returned rate-limit headers.
- Local tokenization estimates are not guaranteed to match service admission or
  billing counts. Record actual usage and cache hits.
- Dashboard rows marked `simulated` are fixtures, not benchmarks.
- Schema-valid processing success is not semantic accuracy. Synthetic label
  accuracy has its own denominator and can be below 100% in a successful run.
- Scheduler metadata has unknown result/error metrics until final reconciliation.
  Remote service request counts are shown separately, not treated as schema-valid results.

UTC timestamps support cross-process correlation. In-process stages use a
monotonic timer. Paused/resumed run wall time includes the pause; stage sums are
not added together to derive wall-clock duration.

The dashboard's custom source reads local or Azure reporting data. Its native
Azure counterpart shows workflow/activity execution, not a complete business
record reconciliation by itself.
