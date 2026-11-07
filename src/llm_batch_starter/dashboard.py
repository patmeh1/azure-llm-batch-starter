from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from azure.core.exceptions import AzureError

from llm_batch_starter.azure_state import AzureState
from llm_batch_starter.config import load_settings
from llm_batch_starter.reporting import summarize_documents, summarize_run
from llm_batch_starter.scheduler import SchedulerAccess
from llm_batch_starter.state import State
from llm_batch_starter.telemetry import elapsed_between


def load_run_history(store: Any) -> list[dict[str, Any]]:
    rows = []
    for run in store.list_runs():
        if isinstance(store, State):
            summary = summarize_run(store, run["id"])
        else:
            summary = {**run, **run.get("summary", {})}
            summary["elapsed_seconds"] = elapsed_between(run["started_at"], run.get("ended_at"))
        rows.append(
            {
                "run_id": run["id"],
                "mode": run["mode"],
                "status": run["status"],
                "source": run.get("source", "unknown"),
                "started_at": run["started_at"],
                "elapsed_seconds": summary["elapsed_seconds"],
                "records": run["record_count"],
                "succeeded": summary.get("succeeded"),
                "failed": summary.get("failed"),
                "pending": summary.get("pending"),
                "errors_429": summary.get("errors_429"),
                "records_per_minute": summary.get("completed_records_per_minute"),
                "model": run.get("metadata", {}).get("model_name", "unknown"),
            }
        )
    return sorted(rows, key=lambda row: row["started_at"], reverse=True)


def load_batch_details(store: Any, run_id: str) -> dict[str, Any]:
    if isinstance(store, SchedulerAccess):
        return store.detail(run_id)
    return summarize_documents(
        store.get_run(run_id),
        store.items("records", run_id),
        store.items("events", run_id),
        store.items("jobs", run_id),
    )


def load_test_history(directory: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(directory.glob("test-summary-*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        rows.append(value)
    return rows


def render_dashboard() -> None:
    st.set_page_config(page_title="LLM Batch Operations", layout="wide")
    st.title("LLM Batch Operations")
    st.caption(
        "Read-only run timing, throughput, retries, and test evidence. No inference is triggered here."
    )
    source = st.sidebar.radio("Data source", ["Local", "Azure scheduler", "Azure Storage"])
    state_path = Path(os.environ.get("LLM_DASHBOARD_STATE", "outputs/state.sqlite3"))
    cloud_config = os.environ.get("LLM_DASHBOARD_CONFIG")
    store: Any = None
    if st.sidebar.button("Refresh metadata"):
        st.rerun()
    try:
        if source != "Local":
            if not cloud_config:
                st.info("Launch with --config pointing to your private Azure configuration.")
                return
            if source == "Azure scheduler":
                store = SchedulerAccess(load_settings(Path(cloud_config)), state_path)
                st.sidebar.caption(
                    "Authorized scheduler metadata only; record artifacts remain in private Storage."
                )
            else:
                store = AzureState(load_settings(Path(cloud_config)))
                st.sidebar.caption(
                    "Azure metadata reads may incur small Storage transaction charges."
                )
        else:
            if not state_path.exists():
                st.info("No local history. Run a workload or launch `llm-batch dashboard --demo`.")
                return
            store = State(state_path, readonly=True)
        history = load_run_history(store)
        if isinstance(store, SchedulerAccess) and store.history_truncated:
            st.info(
                "Showing locally known runs plus the first page of completed scheduler instances. Use the native dashboard for older history."
            )
        if not history:
            st.info("No runs have been recorded.")
            return
        frame = pd.DataFrame(history)
        modes = st.sidebar.multiselect(
            "Processing modes",
            sorted(frame["mode"].unique()),
            default=sorted(frame["mode"].unique()),
        )
        statuses = st.sidebar.multiselect(
            "Statuses", sorted(frame["status"].unique()), default=sorted(frame["status"].unique())
        )
        models = st.sidebar.multiselect(
            "Models", sorted(frame["model"].unique()), default=sorted(frame["model"].unique())
        )
        dates = pd.to_datetime(frame["started_at"], utc=True).dt.date
        first_date = st.sidebar.date_input("Start date", min(dates))
        last_date = st.sidebar.date_input("End date", max(dates))
        frame = frame[
            frame["mode"].isin(modes)
            & frame["status"].isin(statuses)
            & frame["model"].isin(models)
            & (dates >= first_date)
            & (dates <= last_date)
        ]
        if frame.empty:
            st.info("No runs match the selected filters.")
            return
        if (frame["source"] == "simulated").any():
            st.warning("SIMULATED demo rows are illustrations, not measured Azure performance.")
        tab_runs, tab_errors, tab_tests = st.tabs(
            ["Runs & batches", "Errors & retries", "Test timings"]
        )
        with tab_runs:
            st.subheader("Run history")
            st.dataframe(frame, hide_index=True, width="stretch")
            st.bar_chart(frame.set_index("run_id")["elapsed_seconds"])
            st.download_button(
                "Download filtered run CSV",
                frame.to_csv(index=False),
                "run-history.csv",
                "text/csv",
            )
            selected = st.selectbox("Inspect a run", frame["run_id"].tolist())
            detail = load_batch_details(store, selected)
            if detail.get("metrics_availability") == "awaiting_final_reconciliation":
                st.info(
                    "Final reconciliation is not available. Missing counts, usage, and errors are unknown, not zero."
                )
            cols = st.columns(4)
            elapsed = detail.get("orchestration_elapsed_seconds", detail["elapsed_seconds"])
            cols[0].metric("Elapsed seconds", f"{elapsed:.2f}")
            cols[1].metric("Succeeded", detail["succeeded"])
            cols[2].metric(
                "Failed / pending",
                f"{detail['failed']} / {detail['pending']}"
                if detail["failed"] is not None
                else None,
            )
            throughput = detail.get(
                "orchestration_completed_records_per_minute", detail["completed_records_per_minute"]
            )
            cols[3].metric(
                "Completed records/min", f"{throughput:.2f}" if throughput is not None else None
            )
            if "orchestration_completed_records_per_minute" in detail:
                st.caption(
                    f"Elapsed time includes scheduler bootstrap. Application-only elapsed: {detail['elapsed_seconds']:.2f}s."
                )
            token_cols = st.columns(4)
            token_cols[0].metric("Reported input tokens", detail["input_tokens"])
            token_cols[1].metric("Reported output tokens", detail["output_tokens"])
            token_cols[2].metric("Cached tokens", detail["cached_tokens"])
            token_cols[3].metric("Reasoning tokens", detail["reasoning_tokens"])
            if not detail.get("usage_complete", True):
                st.caption(
                    "Usage is missing for some or all responses; displayed totals cover reported usage only."
                )
            if detail["label_accuracy"] is not None:
                st.write(
                    {
                        "Synthetic label accuracy": detail["label_accuracy"],
                        "Labeled result count": detail["accuracy_denominator"],
                    }
                )
                st.caption("Schema-valid processing success is not the same as semantic accuracy.")
            if detail["http_200_latency_p50_seconds"] is not None:
                st.write(
                    {
                        "HTTP 200 attempt p50 seconds": detail["http_200_latency_p50_seconds"],
                        "HTTP 200 attempt p95 seconds": detail["http_200_latency_p95_seconds"],
                    }
                )
            st.caption(detail["cost_note"])
            st.write({"Reported-usage cost estimate (USD)": detail["priced_usage_estimate_usd"]})
            st.subheader("Batch partitions")
            batch_rows = []
            for job in detail["jobs"]:
                start = job.get("submitted_at") or job.get("created_at")
                batch_rows.append(
                    {
                        "partition": job["id"],
                        "status": job["status"],
                        "elapsed_seconds": elapsed_between(start, job.get("completed_at"))
                        if start
                        else None,
                        "estimated_input_tokens": job.get("estimated_tokens"),
                        "service_status": job.get("remote_status"),
                        "service_completed": (job.get("request_counts") or {}).get("completed"),
                        "service_failed": (job.get("request_counts") or {}).get("failed"),
                    }
                )
            st.dataframe(pd.DataFrame(batch_rows), hide_index=True, width="stretch")
            st.subheader("Stage timing aggregates")
            st.json(detail["stage_aggregate_seconds"])
            st.caption(detail["stage_timing_note"])
            if detail["mode"] == "batch":
                st.info(
                    "The Batch service does not provide per-record inference latency here; it is not inferred from batch duration."
                )
            st.download_button(
                "Download run summary JSON",
                json.dumps(detail, indent=2, default=str),
                "run-summary.json",
                "application/json",
            )
        with tab_errors:
            st.subheader("Errors and retry waiting")
            events = store.items("events", selected)
            columns = [
                "at",
                "stage",
                "record_id",
                "partition_id",
                "status_code",
                "error",
                "duration_seconds",
            ]
            errors = [
                {key: item.get(key) for key in columns}
                for item in events
                if item.get("status_code") not in {None, 200} or item["stage"] == "retry_wait"
            ]
            st.metric("HTTP 429 attempts", detail["errors_429"])
            retry_wait = detail["retry_wait_seconds"]
            st.metric(
                "Retry waiting seconds", f"{retry_wait:.2f}" if retry_wait is not None else None
            )
            st.dataframe(pd.DataFrame(errors), hide_index=True, width="stretch")
            failed_records = [
                {key: item.get(key) for key in ("id", "error", "status_code")}
                for item in store.items("records", selected)
                if item["status"] == "failed"
            ]
            st.subheader("Final failed records")
            st.dataframe(pd.DataFrame(failed_records), hide_index=True, width="stretch")
            st.subheader("Observed client request/token rates by UTC minute")
            st.json(detail["per_minute"])
            st.caption(
                "Observed client attempts and successful-response tokens are not the service's complete quota accounting."
            )
            st.download_button(
                "Download error metadata JSON",
                json.dumps(errors, indent=2),
                "errors.json",
                "application/json",
            )
        with tab_tests:
            st.subheader("Test execution history")
            report_dir = Path(st.text_input("Test report directory", "reports"))
            for suite in load_test_history(report_dir):
                st.write(
                    f"{suite['timestamp']} - {suite['status']} - {suite['duration_seconds']:.2f}s"
                )
                st.dataframe(pd.DataFrame(suite["tests"]), hide_index=True, width="stretch")
            st.caption(
                "Generate reports with scripts/run-tests.ps1 or python scripts/run-tests.py."
            )
    except (AzureError, ValueError, OSError, json.JSONDecodeError) as exc:
        st.error(
            f"Cannot read telemetry ({type(exc).__name__}). Check data integrity, identity, and configuration."
        )
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    render_dashboard()
