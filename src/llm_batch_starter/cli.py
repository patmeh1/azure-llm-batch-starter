from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import openai
import typer
from azure.core.exceptions import AzureError
from filelock import Timeout as LockTimeout

from .azure_state import AzureState, create_cloud_run, valid_run_id
from .config import load_settings
from .demo import seed_demo
from .pipeline import run_pipeline
from .prepare import (
    estimate_cost,
    estimate_input_tokens,
    prepare_requests,
    synthetic_target_padding,
)
from .provider import AzureProvider
from .reconcile import is_retryable
from .reporting import summarize_run
from .scheduler import SchedulerAccess
from .state import State
from .synthetic import generate_dataset, read_records, write_records
from .workloads import complex_records, configure_profile, validate_profile_records

app = typer.Typer(
    no_args_is_help=True, help="Reliable Azure OpenAI Batch automation and measurements."
)
ConfigPath = Annotated[Path | None, typer.Option("--config", help="Private JSON settings file.")]


def emit(value: object, *, err: bool = False) -> None:
    typer.echo(json.dumps(value, indent=2, default=str), err=err)


@app.command()
def generate(
    output: Path = Path("outputs/input.jsonl"),
    count: int = 100,
    seed: int = 42,
    target_input_tokens: int = 0,
    baseline_tokens: int = 0,
    multiplier: float = 1,
    config: ConfigPath = None,
    profile: str = "simple",
) -> None:
    """Create reproducible synthetic records; optional input-token target is an estimate."""
    settings, metadata = configure_profile(load_settings(config), profile)
    if multiplier <= 0 or baseline_tokens < 0 or target_input_tokens < 0:
        raise ValueError("Token counts must be nonnegative and multiplier positive")
    target = target_input_tokens or round(baseline_tokens * multiplier)
    if profile == "complex-v2":
        records = complex_records(count, seed, target)
    else:
        reference = synthetic_target_padding(target, settings, seed)
        records = generate_dataset(count, seed=seed, reference_tokens=reference)
    prepare_requests(records, settings)
    write_records(output, records)
    tokens = [estimate_input_tokens(r, settings) for r in records]
    emit(
        {
            "path": str(output),
            "count": count,
            "workload_profile": metadata,
            "target_input_tokens": target,
            "estimated_input_tokens_min": min(tokens),
            "estimated_input_tokens_max": max(tokens),
            "note": "Local tokenizer estimate, not measured API usage. Dataset is entirely synthetic.",
        }
    )


@app.command()
def prepare(
    input: Path,
    output: Path = Path("outputs/prepared"),
    config: ConfigPath = None,
    profile: str = "simple",
) -> None:
    """Validate and split records into actual Batch API JSONL requests, without Azure calls."""
    settings, _ = configure_profile(load_settings(config), profile)
    records = read_records(input)
    validate_profile_records(records, profile)
    partitions = prepare_requests(records, settings)
    output.mkdir(parents=True, exist_ok=False)
    for partition in partitions:
        (output / f"{partition.id}.jsonl").write_bytes(partition.payload)
    emit({"partitions": len(partitions), **estimate_cost(records, settings, batch=True)})


@app.command()
def run(
    input: Path,
    mode: str = "batch",
    config: ConfigPath = None,
    state: Path = Path("outputs/state.sqlite3"),
    output: Path = Path("outputs/runs"),
    wait: bool = True,
    yes: bool = False,
    profile: str = "simple",
) -> None:
    """Run sequential, bounded parallel, or real Batch processing. Paid operation."""
    settings, metadata = configure_profile(load_settings(config), profile)
    records = read_records(input)
    validate_profile_records(records, profile)
    emit(estimate_cost(records, settings, batch=mode == "batch"), err=True)
    if not yes:
        typer.confirm("Submit these records to Azure and incur usage charges?", abort=True)
    summary = run_pipeline(
        records, settings, mode, state, output, wait=wait, workload_profile=metadata
    )
    emit(summary)
    raise typer.Exit(
        0 if summary["status"] == "succeeded" else 2 if summary["status"] == "pending" else 1
    )


@app.command()
def resume(
    run_id: str,
    state: Path = Path("outputs/state.sqlite3"),
    wait: bool = True,
) -> None:
    """Resume an existing local run using its saved inputs/configuration."""
    store = State(state)
    try:
        previous = store.get_run(run_id)
    finally:
        store.close()
    directory = Path(previous["directory"])
    result = run_pipeline(
        read_records(directory / "input.jsonl"),
        load_settings(directory / "config.json"),
        previous["mode"],
        state,
        directory.parent,
        run_id=run_id,
        wait=wait,
    )
    emit(result)
    raise typer.Exit(
        0 if result["status"] == "succeeded" else 2 if result["status"] == "pending" else 1
    )


@app.command("retry-failed")
def retry_failed(
    run_id: str,
    state: Path = Path("outputs/state.sqlite3"),
    yes: bool = False,
) -> None:
    """Create a new run containing only known failed records, never successful/pending items."""
    store = State(state)
    try:
        previous = store.get_run(run_id)
        failed = {
            item["id"]
            for item in store.items("records", run_id)
            if item["status"] == "failed" and is_retryable(item)
        }
    finally:
        store.close()
    if not failed:
        raise ValueError(
            "No retryable failed records; resolve permanent, pending, or ambiguous results separately"
        )
    directory = Path(previous["directory"])
    records = [r for r in read_records(directory / "input.jsonl") if r.id in failed]
    if not yes:
        typer.confirm(f"Retry {len(records)} failed records in a new paid run?", abort=True)
    result = run_pipeline(
        records,
        load_settings(directory / "config.json"),
        previous["mode"],
        state,
        directory.parent,
        parent_run_id=run_id,
        workload_profile=previous.get("workload_profile"),
    )
    emit(result)
    raise typer.Exit(0 if result["status"] == "succeeded" else 1)


@app.command()
def report(run_id: str, state: Path = Path("outputs/state.sqlite3")) -> None:
    store = State(state, readonly=True)
    try:
        emit(summarize_run(store, run_id))
    finally:
        store.close()


@app.command()
def runs(state: Path = Path("outputs/state.sqlite3")) -> None:
    store = State(state, readonly=True)
    try:
        emit(store.list_runs())
    finally:
        store.close()


@app.command("load-test")
def load_test(
    scenario: Annotated[Path, typer.Option("--scenario")],
    config: ConfigPath = None,
    output: Path = Path("outputs/load-tests"),
    yes: bool = False,
) -> None:
    """Preview a bounded load matrix; --yes authorizes paid execution of its real prompts."""
    from .load_testing import Scenario, execute_scenario, plan_scenario

    specification = Scenario.model_validate_json(scenario.read_text(encoding="utf-8"))
    settings = load_settings(config)
    plan = plan_scenario(specification, settings)
    if not yes:
        emit(plan)
        return
    emit(plan, err=True)
    result = execute_scenario(specification, settings, output)
    emit(result)
    raise typer.Exit(
        0 if result["status"] == "passed" else 2 if result["status"] == "pending" else 1
    )


@app.command("load-test-refresh")
def load_test_refresh(report: Path) -> None:
    """Refresh saved cloud stage IDs without submitting additional inference."""
    from .load_testing import refresh_scenario

    result = refresh_scenario(report)
    emit(result)
    raise typer.Exit(
        0 if result["status"] == "passed" else 2 if result["status"] == "pending" else 1
    )


@app.command()
def cancel(
    run_id: str,
    config: ConfigPath = None,
    state: Path = Path("outputs/state.sqlite3"),
    yes: bool = False,
) -> None:
    """Request cancellation of submitted local Batch jobs; collect partial results using resume."""
    store = State(state)
    provider = AzureProvider(load_settings(config))
    try:
        if not yes:
            typer.confirm("Cancel this run's remote Batch jobs?", abort=True)
        for job in store.items("jobs", run_id):
            if job.get("remote_id") and job["status"] != "collected":
                provider.cancel(job["remote_id"])
        emit(
            {
                "run_id": run_id,
                "status": "cancellation_requested",
                "next": "Resume monitoring to reconcile completed and unfinished records.",
            }
        )
    finally:
        provider.close()
        store.close()


@app.command("v4-run")
def v4_run(
    scenario: Annotated[Path, typer.Option("--scenario")],
    config: ConfigPath = None,
    yes: bool = False,
) -> None:
    """Preview or start the matched-quota durable single-versus-three deployment experiment."""
    from .multi_deployment import MultiDeploymentSpec, plan_multi

    spec = MultiDeploymentSpec.model_validate_json(scenario.read_text(encoding="utf-8"))
    settings = load_settings(config)
    plan = plan_multi(spec, settings)
    if not yes:
        emit(plan)
        return
    emit(plan, err=True)
    access = SchedulerAccess(settings)
    try:
        emit(
            {
                "run_id": access.start_multi(spec),
                "status": "queued",
                "note": "The workflow executes in Azure; use v4-status with this same ID.",
            }
        )
    finally:
        access.close()


@app.command("v4-status")
def v4_status(
    run_id: str,
    config: ConfigPath = None,
    wait: bool = False,
    timeout: int = 1800,
    output: Path = Path("outputs/v4"),
) -> None:
    """Monitor the existing Azure-hosted v4 experiment, saving an operational report."""
    if not 1 <= timeout <= 86400:
        raise ValueError("Observer timeout must be 1..86400 seconds; it does not cancel Azure work")
    access = SchedulerAccess(load_settings(config))
    deadline = time.monotonic() + timeout
    directory = output / run_id
    valid_run_id(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        while True:
            run = access.get_run(run_id)
            if run["mode"] != "multi-deployment":
                raise ValueError("This is not a v4 multi-deployment experiment")
            report = access.detail(run_id)
            (directory / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            emit(
                {
                    "run_id": run_id,
                    "status": run["status"],
                    "runtime_status": run["runtime_status"],
                    "progress": report.get("multi_deployment_progress"),
                    "workers": report.get("worker_progress"),
                    "acceptance_status": report.get("acceptance_status"),
                }
            )
            if run["status"] not in {"queued", "pending", "running"}:
                raise typer.Exit(0 if report.get("acceptance_status") == "passed" else 1)
            if not wait or time.monotonic() >= deadline:
                raise typer.Exit(2)
            time.sleep(min(30, max(0, deadline - time.monotonic())))
    finally:
        access.close()


@app.command("v4-export")
def v4_export(report: Path, output: Path) -> None:
    """Export a saved v4 report and bounded synthetic output samples without Azure account context."""
    from .multi_deployment import export_multi

    emit(export_multi(report, output))


@app.command("v3-config")
def v3_config(
    base_config: Path,
    output: Path = Path(".azure/v3-config.json"),
    deployment: str = "gpt5-mini-online",
) -> None:
    """Create a separate private GPT-5 mini configuration; does not deploy or invoke Azure."""
    settings = load_settings(
        base_config,
        model_name="gpt-5-mini",
        model_version="2025-08-07",
        online_deployment=deployment,
        batch_deployment="not-supported-for-v3",
        input_price_per_million=0.275,
        cached_input_price_per_million=0.0275,
        output_price_per_million=2.2,
        rpm=150,
        tpm=150000,
        max_run_cost_usd=75,
        observe_service_throttling=False,
        observation_profile="bounded-burst",
        max_http_attempts=10000,
        run_timeout_seconds=3600,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(settings.model_dump_json(indent=2))
    emit(
        {
            "config": str(output),
            "model": settings.model_name,
            "note": "Private file; verify model deployment, Data Zone SKU, quota, and current prices before use.",
        }
    )


@app.command("v3-run")
def v3_run(
    scenario: Annotated[Path, typer.Option("--scenario")],
    config: ConfigPath = None,
    output: Path = Path("outputs/v3"),
    yes: bool = False,
) -> None:
    """Preview or explicitly run the long GPT-5 mini sequential/five-group comparison."""
    from .reproduction import ReproductionSpec, execute_reproduction, plan_reproduction

    spec = ReproductionSpec.model_validate_json(scenario.read_text(encoding="utf-8"))
    settings = load_settings(config)
    if not yes:
        emit(plan_reproduction(spec, settings))
        return
    result = execute_reproduction(
        spec,
        settings,
        output,
        on_created=lambda path: emit({"experiment": str(path), "status": "checkpoint_created"}),
    )
    emit(result)
    raise typer.Exit(0 if result["status"] == "completed" else 1)


@app.command("v3-status")
def v3_status(experiment: Path) -> None:
    """Read checkpointed progress without inference or a second runner."""
    from .reproduction import reproduction_status

    emit(reproduction_status(experiment))


@app.command("v3-resume")
def v3_resume(experiment: Path, yes: bool = False) -> None:
    """Resume the same saved experiment without resetting budgets or repeating known records."""
    from .reproduction import resume_reproduction

    if not yes:
        typer.confirm(
            "Resume the existing experiment with its original attempt/time/cost limits?", abort=True
        )
    result = resume_reproduction(experiment)
    emit(result)
    raise typer.Exit(0 if result["status"] == "completed" else 1)


@app.command("v3-export")
def v3_export(experiment: Path, output: Path) -> None:
    """Export allowlisted synthetic metrics and terminal outputs without private Azure context."""
    from .reproduction import export_reproduction

    emit(export_reproduction(experiment, output))


@app.command("azure-demo")
def azure_demo(
    config: ConfigPath = None,
    count: int = 10,
    seed: int = 42,
    input: Path | None = None,
    transport: str = "auto",
    target_input_tokens: int = 0,
    yes: bool = False,
    profile: str = "simple",
) -> None:
    """Submit a real synthetic run to the deployed Durable Functions workflow."""
    settings, metadata = configure_profile(load_settings(config), profile)
    if transport not in {"auto", "scheduler", "storage"}:
        raise ValueError("transport must be auto, scheduler, or storage")
    selected_transport = ("storage" if input else "scheduler") if transport == "auto" else transport
    if input is not None and selected_transport == "scheduler":
        raise ValueError(
            "Scheduler transport generates synthetic records; custom files need approved Storage connectivity"
        )
    padding = 0
    if input:
        records = read_records(input)
        validate_profile_records(records, profile)
    elif profile == "complex-v2":
        records = complex_records(count, seed, target_input_tokens)
    else:
        padding = synthetic_target_padding(target_input_tokens, settings, seed)
        records = generate_dataset(count, seed=seed, reference_tokens=padding)
    emit(estimate_cost(records, settings, batch=True), err=True)
    if not yes:
        typer.confirm("Upload and submit to the Azure workflow, incurring charges?", abort=True)
    if selected_transport == "scheduler":
        access = SchedulerAccess(settings)
        try:
            run_id = (
                access.start_complex(count, seed)
                if profile == "complex-v2"
                else access.start_synthetic(count, seed, padding)
            )
        finally:
            access.close()
        result = {"id": run_id, "status": "queued"}
    else:
        result = create_cloud_run(records, settings, workload_profile=metadata)
    emit(
        {
            "run_id": result["id"],
            "status": result["status"],
            "transport": selected_transport,
            "next": f"llm-batch azure-status {result['id']} --config <private-config>",
            "note": "The workflow continues in Azure after this CLI exits.",
        }
    )


@app.command("azure-status")
def azure_status(
    run_id: str,
    config: ConfigPath = None,
    wait: bool = False,
    timeout: int = 300,
    poll_seconds: int = 15,
    output: Path = Path("outputs/azure"),
    transport: str = "scheduler",
) -> None:
    """Read cloud progress and collect final artifacts; timeout does not cancel the cloud job."""
    if timeout <= 0 or poll_seconds < 5:
        raise ValueError("timeout must be positive and poll_seconds at least 5")
    if transport not in {"scheduler", "storage"}:
        raise ValueError("transport must be scheduler or storage")
    store = (
        SchedulerAccess(load_settings(config))
        if transport == "scheduler"
        else AzureState(load_settings(config))
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            run = store.get_run(run_id)
            emit(run)
            if run["status"] not in {"queued", "running", "pending"}:
                directory = output / run_id
                directory.mkdir(parents=True, exist_ok=True)
                if isinstance(store, SchedulerAccess):
                    (directory / "summary.json").write_text(
                        json.dumps(store.detail(run_id), indent=2), encoding="utf-8"
                    )
                    typer.echo(
                        "Operational summary saved. Full record artifacts remain in private Storage.",
                        err=True,
                    )
                else:
                    filenames = ["summary.json", "results.jsonl", "results.csv"]
                    if run.get("workload_profile", {}).get("name") == "complex-v2":
                        filenames += ["analysis.csv", "prompt.md"]
                    for filename in filenames:
                        (directory / filename).write_bytes(store.get_blob(f"{run_id}/{filename}"))
                raise typer.Exit(0 if run["status"] == "succeeded" else 1)
            if not wait or time.monotonic() + poll_seconds >= deadline:
                raise typer.Exit(2)
            time.sleep(poll_seconds)
    finally:
        store.close()


@app.command("azure-recover")
def azure_recover(run_id: str, config: ConfigPath = None, yes: bool = False) -> None:
    """Recover an investigated workflow failure using saved jobs, not replacement inference."""
    if not yes:
        typer.confirm(
            "After fixing the workflow failure, resume its saved partitions in a new orchestration?",
            abort=True,
        )
    access = SchedulerAccess(load_settings(config))
    try:
        emit(
            {
                "run_id": access.recover(run_id),
                "parent_run_id": run_id,
                "status": "recovery_requested",
            }
        )
    finally:
        access.close()


@app.command("azure-cancel")
def azure_cancel(
    run_id: str, config: ConfigPath = None, yes: bool = False, transport: str = "scheduler"
) -> None:
    if not yes:
        typer.confirm(
            "Request cancellation of this cloud run? In-flight work may still complete.",
            abort=True,
        )
    if transport not in {"scheduler", "storage"}:
        raise ValueError("transport must be scheduler or storage")
    store = (
        SchedulerAccess(load_settings(config))
        if transport == "scheduler"
        else AzureState(load_settings(config))
    )
    try:
        store.get_run(run_id)
        if isinstance(store, SchedulerAccess):
            store.request_cancel(run_id)
        else:
            store.enqueue(run_id, action="cancel")
        emit({"run_id": run_id, "status": "cancellation_requested"})
    finally:
        store.close()


@app.command("azure-retry-failed")
def azure_retry_failed(run_id: str, config: ConfigPath = None, yes: bool = False) -> None:
    """Create a new cloud run containing confirmed transient failures only."""
    if not yes:
        typer.confirm("Create a new paid run for eligible failed records only?", abort=True)
    store = SchedulerAccess(load_settings(config))
    try:
        child = store.retry_failed(run_id)
        emit({"run_id": child, "parent_run_id": run_id, "status": "queued"})
    finally:
        store.close()


@app.command()
def dashboard(
    state: Path = Path("outputs/state.sqlite3"),
    demo: bool = False,
    config: ConfigPath = None,
    port: int = 8501,
) -> None:
    """Launch the read-only local dashboard. --demo creates explicitly simulated history."""
    if demo:
        seed_demo(state)
    script = Path(__file__).with_name("dashboard.py")
    env = os.environ.copy()
    env["LLM_DASHBOARD_STATE"] = str(state.resolve())
    if config:
        env["LLM_DASHBOARD_CONFIG"] = str(config.resolve())
    code = subprocess.call(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(script),
            "--server.address",
            "127.0.0.1",
            "--server.port",
            str(port),
            "--browser.gatherUsageStats",
            "false",
            "--server.headless",
            "true",
        ],
        env=env,
    )
    raise typer.Exit(code)


def main() -> None:
    try:
        app()
    except (AzureError, openai.OpenAIError) as exc:
        typer.echo(
            f"Azure operation failed: {type(exc).__name__}. Check identity, quota, and deployment settings.",
            err=True,
        )
        raise SystemExit(1) from None
    except (ValueError, RuntimeError, OSError, LockTimeout) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
