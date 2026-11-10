# Customer-facing comparison site

Public page: **https://patmeh1.github.io/azure-llm-batch-starter/**

The site now uses real, independently addressable HTML pages rather than a
single long page with hash-only navigation. Every tab loads its own page, marks
the current location, and supports direct links, refresh, browser back, and
navigation without JavaScript.

| Page | Purpose |
|---|---|
| [Overview](https://patmeh1.github.io/azure-llm-batch-starter/) | The story: capacity and pacing, complex-task accuracy, then GPT-5 mini grouping |
| [Compare versions](https://patmeh1.github.io/azure-llm-batch-starter/comparison.html) | Side-by-side configuration and outcome tables across v1/v2/v3 |
| [V1 baseline](https://patmeh1.github.io/azure-llm-batch-starter/v1.html) | GPT-5.4 mini compact, token-stress, and recovery findings |
| [V2 complexity](https://patmeh1.github.io/azure-llm-batch-starter/v2.html) | Same GPT-5.4 mini build, complex prompts, and failed semantic gates |
| [V3 GPT-5 mini](https://patmeh1.github.io/azure-llm-batch-starter/v3.html) | Different model, same complex prompt, fixed groups, and incomplete evidence |
| [What works](https://patmeh1.github.io/azure-llm-batch-starter/approach.html) | Decisions supported by the evidence, not a model ranking |
| [Resources](https://patmeh1.github.io/azure-llm-batch-starter/resources.html) | Quickstart, prompts, guides, code, architecture, outputs, and releases |
| [Methodology](https://patmeh1.github.io/azure-llm-batch-starter/methodology.html) | What was held constant, changed, and cannot be concluded |

V1/v2 explicitly identify **GPT-5.4 mini 2026-03-17**; v3 identifies
**GPT-5 mini 2025-08-07**. The comparison separates prompt/task complexity,
record counts, quota, pacing, output ceilings, execution modes, service-Batch
availability, timing, 429s, quality, and token-cost estimates. It does not merge
different workloads into a speed leaderboard.

Content remains customer-neutral and synthetic. Old shared root links such
as `/#v2` redirect to their dedicated pages when JavaScript is available.

## Data sources and freshness

| Page content | Committed source |
|---|---|
| V1 compact, token-stress, and scale/recovery results | `docs/live-results.json` |
| V2 complex-prompt timing and semantic acceptance failures | `docs/complex-v2-results.json` |
| V3 timestamped progress or terminal results | `docs/v3-results.json` |
| V3 example outputs | `docs/v3-partial-output-sample.json` |
| Downloadable prompts | The four Markdown files under `prompts` |
| Illustrative dashboard image | `docs/images/complex-dashboard-demo.png`, labeled SIMULATED |

This is a **static publication**, not a live Azure monitor. It does not read
the running experiment, private storage, credentials, or API endpoints. V3
always shows its evidence timestamp; incomplete metrics are not presented as
final performance results.
When the runner is not active, the public snapshot labels the run
`interrupted` and records `last_activity_at` separately from the time of
inspection. A recently checked stale checkpoint is not live progress.

The HTML contains pre-rendered comparisons and inline styles/scripts/image
data, so its core content works without JavaScript. Theme and clipboard controls
are optional enhancements. There are no analytics, client-side storage, forms,
or network API calls; the page's Content Security Policy prohibits connections.
Ordinary links take readers to public evidence and documentation.

## Build and preview

From the repository root:

```powershell
npm run build:site
python -m http.server 8771 --bind 127.0.0.1 --directory _site
```

Open `http://127.0.0.1:8771`. Stop the temporary server before running browser
tests, which manage their own server on that port.

The builder is Python standard-library code. `site/layout.html` is shared by
the eight templates in `site/pages`. It reads a fixed allowlist and
produces only the eight HTML pages, public evidence JSON, Markdown prompts, and an
illustrative image under ignored `_site`. It rejects unexpected output files,
common private-context fields/values, inconsistent outcome counts, and
contradictory pending/final flags rather than silently publishing them.
All page and fragment links are validated across the generated output, and
model/prompt identity checks guard against silently relabeling historical data.

These guards are not a complete data-loss-prevention system. Review all new
source data and output examples before committing or sharing them.

## Tests

```powershell
python scripts\run-tests.py -q tests\test_site.py
npm run test:site
python scripts\normalize-browser-report.py --category site
```

Use the existing Playwright setup described in [testing](testing.md). On
Windows with Edge installed, set `$env:BROWSER_CHANNEL='msedge'`. Tests cover
evidence values, explicit interrupted/pending states, terminal-state rendering,
escaping/privacy guards, cross-page fragments, each tab's direct route and
active state, reload/back behavior, legacy hash links, mobile tables, themes
across navigation, downloads, clipboard failures, and no-JavaScript access.

## Publishing

`.github/workflows/pages.yml` builds and browser-tests the site on a clean
runner, then publishes **only `_site`** using the official GitHub Pages actions.
Build has read-only repository/Pages access; deploy has Pages and short-lived
OIDC permissions. No Azure credentials, subscriptions, or paid inference are
configured in this workflow.

GitHub Pages is configured to use **GitHub Actions**. Pushes to `main` affecting
the website, public docs/evidence, prompts, or build/test configuration trigger
publication. Manual `workflow_dispatch` is also available. Deployment uses the
`github-pages` environment and a serialized deployment concurrency group.

## Updating the v3 results after the long run

On the authorized experiment host, inspect the existing checkpoint; do not
start another paid run to update the website:

```powershell
llm-batch v3-status outputs\v3\EXPERIMENT\experiment.json
llm-batch v3-export outputs\v3\EXPERIMENT\experiment.json outputs\v3-public
```

Review the allowlisted `summary.json`, copy it to `docs/v3-results.json`, and
update `docs/v3-results.md` to match. Keep `results_final: false` while execution
is pending or interrupted. Only a genuinely terminal experiment may set it
true; the exporter derives this from the recorded state. If process observation
shows an interruption while the saved checkpoint still says running, preserve
the original checkpoint and explicitly annotate the public snapshot with the
observed interruption and last activity. Do not silently restart paid work to
make the website appear current.

When final results are ready, audit and attach the complete synthetic CSV/JSONL
exports to the v3 release, update its notes/status, and commit the reviewed
evidence. The Pages workflow republishes automatically; the v3 section switches
from progress-only rendering to final timing and quality tables.

Do not overwrite historical v1/v2 measurements with a different workload.
If the model, scenario, or scope changes, update the narrative and corresponding
build assertions deliberately instead of relabeling old results.
