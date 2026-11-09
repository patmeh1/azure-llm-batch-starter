# Customer-facing comparison site

Public page: **https://patmeh1.github.io/azure-llm-batch-starter/**

The site brings together v1/v2/v3 comparisons, methodology, capacity and
throttling findings, quality limitations, prompts, scenarios, architecture,
guides, releases, and output examples. It contains only customer-neutral
synthetic material already approved for the public repository.

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

The builder is Python standard-library code. It reads a fixed allowlist and
produces only the HTML, public evidence JSON, Markdown prompts, and an
illustrative image under ignored `_site`. It rejects unexpected output files,
common private-context fields/values, inconsistent outcome counts, and
contradictory pending/final flags rather than silently publishing them.

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
evidence values, explicit pending states, automatic terminal-state rendering,
escaping/privacy guards, internal asset links, mobile overflow, both themes,
downloads, clipboard failure handling, and no-JavaScript access.

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
true; the exporter derives this from the recorded state.

When final results are ready, audit and attach the complete synthetic CSV/JSONL
exports to the v3 release, update its notes/status, and commit the reviewed
evidence. The Pages workflow republishes automatically; the v3 section switches
from progress-only rendering to final timing and quality tables.

Do not overwrite historical v1/v2 measurements with a different workload.
If the model, scenario, or scope changes, update the narrative and corresponding
build assertions deliberately instead of relabeling old results.
