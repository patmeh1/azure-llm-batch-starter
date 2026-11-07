# Local processing tutorial

The local path runs the coordinator on your machine. It is separate from the
Azure-hosted workflow described in [the Azure tutorial](azure-demo.md).

## Generate and inspect synthetic input

```powershell
llm-batch generate --count 100 --seed 42 --output outputs\tickets.jsonl
llm-batch prepare outputs\tickets.jsonl --output outputs\prepared
```

Each record contains `id`, `text`, and optional `expected_label`. The expected
label is for evaluation and is **not** included in the model request.
Generation refuses to overwrite an existing input file.

## Configure

Use the private `.azure\demo-config.json` produced by deployment, or write your
own JSON Settings file outside Git. Set `prompt` and `output_schema` to customize
the task. The schema must be accepted by your model's Structured Outputs API.

Configuration includes model/deployment names, the endpoint, token/context
limits, per-run estimated cost ceiling, timeout, concurrency, and rate budgets.
Use deployment names for API routing; model names describe the actual model.
Default authentication is the Azure CLI identity. Use the `managed_identity`
mode only in a corresponding Azure environment.

The default rates are conservative configured estimates, not billing records.
Review them against current regional pricing before real runs.

## Compare modes on the same input

```powershell
llm-batch run outputs\tickets.jsonl --mode sequential --config .azure\demo-config.json
llm-batch run outputs\tickets.jsonl --mode parallel --config .azure\demo-config.json
llm-batch run outputs\tickets.jsonl --mode batch --config .azure\demo-config.json
```

Each command requires paid-operation confirmation unless `--yes` is supplied.
Do not compare different input datasets or model versions and attribute the
difference solely to concurrency.

The local online limiter coordinates threads in one process. A per-deployment
file lock prevents simultaneous local coordinators using the same lock directory.
It is **not a distributed limiter** across machines.

The Batch mode uses the actual Azure OpenAI Batch API, not multiple calls bundled
inside one prompt. File partitioning respects configured byte, record, and
enqueued-token budgets.

## Resume, retry, and export

```powershell
llm-batch runs
llm-batch report YOUR_RUN_ID
llm-batch resume YOUR_RUN_ID
llm-batch retry-failed YOUR_RUN_ID
```

Saved inputs/configuration must match to resume. Successful records are not
knowingly resubmitted. `retry-failed` creates a new run containing only known
failed records; it does not retry pending/ambiguous submissions.

Local exports include `summary.json`, `results.jsonl`, and `results.csv` under
`outputs\runs\YOUR_RUN_ID`. CSV stores arbitrary validated output in a JSON
`result` column. Parse that column to flatten a task-specific schema.

Close/restart the local coordinator only with its state and input artifacts
preserved. Already-submitted remote Batch jobs continue, but prepared partitions
not yet submitted need a local resume. For a workflow that continues submission
and monitoring without your machine, use the Azure-hosted path.
