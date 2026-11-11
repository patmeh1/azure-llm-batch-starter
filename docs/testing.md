# Testing and evidence

## Offline checks

```powershell
python -m pip install -e ".[dev,cloud]"
python -m ruff check src services tests scripts
python -m mypy src\llm_batch_starter
python scripts\run-tests.py -q
python -m build --no-isolation
python scripts\verify-release.py
python -m pip_audit --disable-pip --no-deps -r requirements.lock --progress-spinner off
```

The timed runner emits JUnit XML plus `reports\test-summary-*.json`, including
failures and skipped tests, then preserves pytest's exit code. The dashboard
reads the normalized JSON reports. CI uploads reports even when tests fail.

Release verification inspects the actual source/wheel archives, including
shell-script LF bytes on Windows builds, required files, private-artifact
exclusions, and the CLI entry point.

Coverage includes deterministic input generation, duplicate IDs, input limits,
partitioning, retry header parsing, refusal/truncation handling, missing/duplicate
results, local orchestration, quota reservations, dashboard rendering, and
whole-run timing.
V2 adds catalog/Markdown integrity, all thirteen complex case families, gold
label exclusion from requests, semantic-versus-schema checks, legacy-record
fingerprint compatibility, budget previews, and a native packaging-hook test.

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

Hosted browser CI and the Pages build use `ubuntu-24.04` with the runner's
preinstalled Chrome (`BROWSER_CHANNEL=chrome`). Both verify the browser version
and run the full browser suites; neither depends on an additional `apt` install
of Playwright's system dependencies. A missing browser fails the job rather
than skipping verification.

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

For complex synthetic prompts, start with the dry-run
`llm-batch load-test --scenario scenarios\complex-v2-smoke.json --config .azure\demo-config.json`.
Follow the [v2 tutorial](complex-prompts.md) before enabling paid execution.
The load-test gate includes quality thresholds and can fail even when every
API response passes its JSON schema.
The manually dispatched, disabled-by-default live workflow also offers a
`complex-v2` profile, selecting thirteen cases and the same quality checks.
It still requires the protected Azure environment and explicit charge approval;
no credentials or recurring paid automation are configured by this repository.

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

See the [measured live validation report](live-validation.md) and its sanitized
JSON evidence. Passing offline CI alone does not establish Azure deployment or
inference success.
The [v2 complex report](complex-v2-validation.md) separately records the
13-case live comparison and its semantic acceptance failures.

V3 adds tests for exact contiguous 5x340 grouping, parallel groups with ordered
records, unchanged v2 prompt hashes, budget/deadline preservation across resume,
explicit ambiguous online outcomes, and US online-only preflight behavior.
Its full live experiment is separately labeled in [v3 results](v3-results.md);
passing offline tests is not a claim that the long run has completed.

The [customer-facing GitHub Pages site](customer-site.md) has a separate
standard-library builder and Playwright suite (`npm run test:site`). Its
normalized timing reports use `--category site`. These tests do not read
private run state or call Azure.

V4 adds a matched-quota dry-run and mocked tests for equal 150K aggregate
allocation, immutable prompt identity, target selection/cooldowns,
service-header parsing, four total attempts across targets, original
deadlines and cost reservations, duplicate activity delivery, uncertain
paid intents, cancellation, lease retention, and full two-arm reconciliation.
The new workflow is exercised through generator-level durable timer tests,
not a replacement custom orchestration engine.

The actual Azure [v4 pilot](v4-results.md) covers 65 records per
arm on four actual GPT-5 mini deployments. Completed durable execution,
schema-valid results, and semantic acceptance are reported separately.
The supplied 1,700-record scenario is **not live-validated** by that pilot.

Website tests cover all nine real routes, four version columns, matched
aggregate allocations, per-target/arm count reconciliation, incomplete
metrics, and privacy/model/prompt guards. They do not silently relabel
historical v1-v3 evidence.

The v4 publication was checked with 156 Python tests, 10 website browser
scenarios, three dashboard browser scenarios, lint/type checks, and actual
source/wheel archive verification. The Python dependency audit passed.
A full npm advisory audit was rate-limited with HTTP429 by the configured
package feed; no clean full npm audit is claimed. That development-tool
request is unrelated to the Azure inference429 counts in the pilot evidence.

V5 tests pre-dispatch token/request reservations, shared-queue work assignment,
per-target exclusivity, cooldown/retry idempotence, finite header feedback,
temporary-limit recovery, original deadlines, warm-up accounting, and
management-plane topology drift. A full 520-record four-attempt ledger is
serialized and checked below the scheduler's 1 MiB state limit with headroom.

Exact-duration fixtures verify arithmetic means and their denominators
independently of elapsed job time. Paid-response tests cover deadline and
cancellation reconciliation without another SDK call. Website tests distinguish
pending from final averages and refuse claims based on missing records,
inconsistent sample counts, or changed five-target allocations. None of these
fixtures is published as measured Azure performance.
