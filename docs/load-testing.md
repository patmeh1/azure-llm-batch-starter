# Bounded input-token, TPM/RPM, and 429 experiments

The default application paces online requests. An explicit observation mode is
available to measure how a **dedicated, authorized test deployment** behaves
when concurrency increases. Do not use it against an unrelated/shared production
endpoint.

For active taxonomy/rule complexity instead of reference-text padding, use
the [v2 complex-prompt scenarios](complex-prompts.md). Their Markdown prompts
are sent in the actual requests and scored against withheld synthetic gold
labels. The two workload profiles are not interchangeable benchmarks.

## Choose a defensible baseline

Use measured API `prompt_tokens` from your own workload where possible. If only
source files are available, tokenize them locally and label the result as a
file-based estimate. Include the actual instructions, schema, one record, and
any repeated history. Never upload real customer content to this public demo.

Generate unrelated synthetic text at twice a chosen estimated input budget:

```powershell
llm-batch generate --count 6 --baseline-tokens 37000 --multiplier 2 --output outputs\large.jsonl
```

This example's number is illustrative. The CLI reports estimated actual minimum
and maximum input tokens after assembling the request. Live responses supply the
authoritative observed usage for each successful request.

## Observe a bounded burst

Review your private configuration and cost allowance first:

```powershell
python scripts\stress.py --config .azure\demo-config.json --baseline-tokens 37000 --multiplier 2 --count 6 --concurrency 2 --observe-service-throttling --yes
```

The experiment is limited to 30 HTTP attempts, at most 30 records, a 180-second
run admission window, and one attempt per record. Allowed concurrency is 1, 2,
or 5. Already-running requests can finish after the admission window; request
timeouts bound that additional time. No unbounded retry loop is used.

To compare paced behavior, omit `--observe-service-throttling`. Ensure the
configured TPM is large enough for one request plus its output budget.
One oversized request cannot be fixed by sleeping forever.

## Interpret results

Open the dashboard and compare:

- Whole-run elapsed time and validated records/minute.
- Returned token/request remaining/limit headers.
- 429 attempts versus final failed records.
- Input, output, cached, and reasoning tokens.
- Retry/admission waiting versus HTTP attempt duration.

Intentional stress can produce failed records. `experiment_completed` means
measurement finished, **not** that all inference succeeded. The underlying run
continues to show its actual failed/partial status.

Short bursts are not sustained-throughput measurements. A run's displayed
records/minute over a few seconds must not be extrapolated to a long backlog.
Cooldown intervals between separate experiments are not part of each run's
wall time. Use `--vary-prefix` to contrast stable-prefix reuse with varying
early input, and inspect actual cache usage rather than assuming a miss.

For compact online records, compare a conservative client RPM budget with a
headroom-aware budget based on the actual service headers:

```powershell
python scripts\benchmark.py --config .azure\demo-config.json --count 100 --concurrency 5 --rpm 120 --yes
```

Only choose 120 RPM when your deployment actually supports it. Flat throughput
at a shared client pacing limit is not evidence that the model cannot scale.

If quota and budget permit, change the existing online deployment allocation
through IaC and repeat the same bounded experiment:

```powershell
azd env set ONLINE_CAPACITY 600
azd provision --preview --no-prompt
azd provision --no-prompt
```

Capacity units are model-specific. Confirm the effective TPM/RPM in response
headers; do not assume the conversion for an arbitrary model. Increasing
Standard allocation is not buying PTUs, but it enables more billable traffic.
Never consume another workload's allocation or switch deployment type silently.

Use the same model, version, records, generation settings, and deployment
allocation for comparisons. A stable repeated prefix can create cache hits;
record them rather than attributing all changes to concurrency.

Larger test matrices and a 1,700-record doubled-token run require a new cost
estimate. A compact 1,700-record correctness test and a small high-token stress
test measure different things.
