# Dashboard

```powershell
llm-batch dashboard --demo
llm-batch dashboard --config .azure\demo-config.json
```

Open `http://127.0.0.1:8501`. The first command seeds simulated local history;
the second makes the Azure scheduler and Storage metadata sources available using your selected CLI
identity. The dashboard does not submit or cancel jobs.

Use **Azure scheduler** for synthetic cloud runs when direct Storage access is
restricted. Full record artifacts remain private. The view includes locally
remembered run IDs and a bounded page of completed scheduler instances; the
native scheduler dashboard provides broader history. **Azure Storage** requires
approved direct connectivity and provides the full persisted metadata view.

- **Runs & batches:** run history, per-run duration, per-partition status and
  duration, stage aggregates, throughput, and CSV/JSON downloads.
- **Errors & retries:** HTTP status codes, 429 counts, retry waiting, and
  observed request/token rates. Raw prompts and response bodies are not shown.
- **Test timings:** normalized test-suite reports generated under `reports`.

Use **Refresh metadata** while a run is active. Unknown data is displayed as
unavailable; a stale view is not evidence that a job stopped.

The cloud-native Durable Task Scheduler dashboard is available through the
Azure Portal scheduler/task hub. It provides lower-level orchestration/activity
history and administrative operations under Azure RBAC.

The custom dashboard binds to localhost. Hosting it for other users requires a
separate authentication/networking design; do not bind it publicly to expose
private operational data.

Restart a running dashboard after upgrading application modules. Development
hot reload is not a substitute for testing the newly installed package.
