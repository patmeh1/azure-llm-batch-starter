# Testing and evidence

## Offline checks

```powershell
python -m pip install -e ".[dev,cloud]"
python -m ruff check src services tests scripts
python -m mypy src\llm_batch_starter
python scripts\run-tests.py -q
python -m build --no-isolation
python -m pip_audit --disable-pip --no-deps -r requirements.lock --progress-spinner off
```

The timed runner emits JUnit XML plus `reports\test-summary-*.json`, including
failures and skipped tests, then preserves pytest's exit code. The dashboard
reads the normalized JSON reports. CI uploads reports even when tests fail.

Coverage includes deterministic input generation, duplicate IDs, input limits,
partitioning, retry header parsing, refusal/truncation handling, missing/duplicate
results, local orchestration, quota reservations, dashboard rendering, and
whole-run timing.

Mocks/fixtures verify logic, **not Azure deployment success**.

Browser tests use the actual Streamlit application:

```powershell
npm ci --ignore-scripts --no-audit --no-fund
npx playwright install chromium
npm run test:browser
python scripts\normalize-browser-report.py
```

On a Windows development machine with Edge already installed, set
`$env:BROWSER_CHANNEL='msedge'` instead of downloading Chromium. The tests start
and stop their own synthetic-only server. Their duration/status reports appear
in the dashboard alongside Python tests.

The scheduler adapter is type-checked. A narrow mypy override skips the
upstream `durabletask.task` generator implementation, which triggers an internal
mypy 1.20 error; application code and public client interfaces remain checked.

## Real Azure smoke test

After following the deployment tutorial and approving costs:

```powershell
python scripts\live-smoke.py --config .azure\demo-config.json --count 10 --timeout 900 --yes
```

The script uses the authenticated scheduler-native Durable workflow and actual
Batch jobs. The optional Storage transport uses the queue starter. Exit `2`
means still pending, not completed or cancelled. Continue
monitoring the returned run ID.

Follow with a same-data 100-record mode comparison and, when affordable, a
1,700-record cloud correctness run. Keep large-input stress runs separately
identified; see [load testing](load-testing.md).

## Reproducibility

Record model/version/deployment type, dataset seed, prompt/schema fingerprints,
concurrency/rate settings, token distributions, cache usage, software versions,
and all failures. Do not publish endpoint names, account IDs, raw Azure logs, or
real input data.

`requirements.lock` records a concrete resolved development environment.
`pyproject.toml` declares supported cross-platform ranges; the Functions service
has its own deployment requirements. Validate on your OS instead of assuming
one platform's lock proves compatibility everywhere.

Docker/DTS emulator testing is optional and is not required for the basic demo.
Real cloud validation remains required for claims about the deployed workflow.

Published live benchmark evidence belongs in a separately labeled sanitized
report. Until such evidence is present, do not infer that a particular
subscription/region completed a live run from passing offline CI alone.
