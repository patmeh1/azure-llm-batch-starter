from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

import openai
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from .azure_state import AzureState, valid_run_id
from .complex_workload import generate_complex
from .config import Settings, load_settings
from .load_testing import Scenario, Stage, quality_gate
from .multi_deployment import (
    ARMS,
    MultiDeploymentSpec,
    error_indication,
    generation_metadata,
    plan_multi,
    public_targets,
    retry_seconds,
    targets,
    v4_settings,
)
from .prepare import estimate_input_tokens
from .provider import AzureProvider, rate_headers
from .quality import annotate_result
from .reconcile import validate_completion
from .reporting import analysis_csv, mean, percentile, results_csv, summarize_documents
from .synthetic import Record
from .telemetry import canonical_json, elapsed_between, event, safe_error, utc_now


class MultiActivities:
    spec_model = MultiDeploymentSpec
    arms: tuple[str, ...] = ARMS
    comparison_arms: tuple[str, ...] = ARMS
    experiment_mode = "multi-deployment"
    manifest_name = "v4-manifest.json"

    def __init__(
        self,
        settings: Settings | None = None,
        store: Any = None,
        provider_factory: Callable[[Settings], Any] = AzureProvider,
    ):
        self.settings, self.profile = self.configure_settings(
            settings or load_settings(auth_mode="managed_identity")
        )
        self.owns_store = store is None
        self.store = store if store is not None else AzureState(self.settings)
        self.provider_factory = provider_factory
        self.manifests: dict[str, dict[str, Any]] = {}

    def configure_settings(self, settings: Settings) -> tuple[Settings, dict[str, Any]]:
        return v4_settings(settings)

    def make_plan(self, spec: MultiDeploymentSpec) -> dict[str, Any]:
        return plan_multi(spec, self.settings)

    def generate_records(self, spec: MultiDeploymentSpec) -> list[Record]:
        return generate_complex(spec.count, spec.seed)

    def arm_count(self, spec: MultiDeploymentSpec, arm: str) -> int:
        return spec.count

    def arm_timeout(self, spec: MultiDeploymentSpec, arm: str) -> int:
        return spec.stage_timeout_seconds

    def target_map(self, arm: str) -> dict[str, dict[str, Any]]:
        return targets(arm)

    def public_target_map(self, arm: str) -> list[dict[str, Any]]:
        return public_targets(arm)

    def manifest_extra(self, options: dict[str, Any]) -> dict[str, Any]:
        return {}

    def final_extra(self, root: str, summary: dict[str, Any]) -> dict[str, Any]:
        return {}

    def provider_for(self, settings: Settings) -> Any:
        return self.provider_factory(settings)

    def release_provider(self, provider: Any) -> None:
        provider.close()

    def timing_fields(self, stage_id: str, start: str, end: str) -> dict[str, Any]:
        return {
            "started_at": start,
            "ended_at": end,
            "elapsed_seconds": elapsed_between(start, end),
        }

    def close(self) -> None:
        if self.owns_store:
            self.store.close()

    def _put_once(self, name: str, payload: bytes) -> None:
        try:
            self.store.put_blob(name, payload)
        except ResourceExistsError:
            if self.store.get_blob(name) != payload:
                raise ValueError("Existing v4 artifact has conflicting content") from None

    def _manifest(self, root: str) -> dict[str, Any]:
        root = valid_run_id(root)
        if root not in self.manifests:
            value = json.loads(self.store.get_blob(f"{root}/{self.manifest_name}"))
            if (
                value["workload_profile"]["prompt_bundle_sha256"]
                != self.profile["prompt_bundle_sha256"]
            ):
                raise ValueError("V4 worker prompt changed during a running experiment")
            self.spec_model.model_validate(value["specification"])
            self.manifests[root] = value
        return self.manifests[root]

    def _stage_id(self, root: str, arm: str) -> str:
        if arm not in self.arms:
            raise ValueError("Unsupported v4 arm")
        return valid_run_id(f"{valid_run_id(root)}-{arm}")

    def _record(self, root: str, index: int) -> Record:
        values = self._manifest(root)["records"]
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(values):
            raise ValueError("Invalid v4 record index")
        return Record.model_validate(values[index])

    def bootstrap(self, options: dict[str, Any]) -> dict[str, Any]:
        root = valid_run_id(options["run_id"])
        spec = self.spec_model.model_validate(options["specification"])
        plan = self.make_plan(spec)
        if (
            options["workload_profile"]["prompt_bundle_sha256"]
            != self.profile["prompt_bundle_sha256"]
        ):
            raise ValueError("V4 client and worker Markdown/schema bundles differ")
        if options["expected_model"] != [self.settings.model_name, self.settings.model_version]:
            raise ValueError("V4 client and worker model/version differ")
        if options["count"] != spec.count * 2:
            raise ValueError("V4 expected output count differs from the two-arm scenario")
        records = self.generate_records(spec)
        estimated = [estimate_input_tokens(record, self.settings) for record in records]
        request_costs = [
            (
                tokens * self.settings.input_price_per_million
                + self.settings.max_output_tokens * self.settings.output_price_per_million
            )
            / 1_000_000
            for tokens in estimated
        ]
        manifest = {
            "records": [record.model_dump() for record in records],
            "specification": spec.model_dump(),
            "workload_profile": self.profile,
            "estimated_tokens": estimated,
            "request_costs": request_costs,
            "generation": generation_metadata(self.settings),
            "plan": plan,
            **self.manifest_extra(options),
        }
        payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
        self._put_once(f"{root}/{self.manifest_name}", payload)
        self._put_once(f"{root}/prompt.md", (self.settings.prompt + "\n").encode())
        fingerprint = hashlib.sha256(payload).hexdigest()
        for arm in (None, *self.arms):
            run_id = root if arm is None else self._stage_id(root, arm)
            run = {
                "id": run_id,
                "mode": self.experiment_mode if arm is None else "parallel",
                "status": "queued",
                "started_at": utc_now(),
                "ended_at": None,
                "record_count": spec.count * 2 if arm is None else self.arm_count(spec, arm),
                "fingerprint": fingerprint,
                "source": "live-azure",
                "metadata": generation_metadata(self.settings),
                "workload_profile": self.profile,
                "arm": arm,
                "parent_run_id": root if arm else None,
            }
            try:
                self.store.save_run(run, create=True)
            except ResourceExistsError:
                if self.store.get_run(run_id)["fingerprint"] != fingerprint:
                    raise ValueError("V4 run ID has a different saved input contract") from None
        self.manifests[root] = manifest
        return {
            "specification": spec.model_dump(),
            "request_costs": request_costs,
            "request_tokens": [tokens + self.settings.max_output_tokens for tokens in estimated],
            "workload_profile": self.profile,
            "conservative_all_attempts_estimate_usd": plan[
                "conservative_all_attempts_estimate_usd"
            ],
        }

    def begin_arm(self, root: str, arm: str) -> dict[str, Any]:
        run = self.store.get_run(self._stage_id(root, arm))
        if run["status"] == "queued":
            run.update(status="running", started_at=utc_now())
            self.store.save_run(run)
        parent = self.store.get_run(root)
        if parent["status"] == "queued":
            parent["status"] = "running"
            self.store.save_run(parent)
        return {"arm": arm, "started_at": run["started_at"]}

    def _start_record(self, stage_id: str, record: Record, ready_at: str | None = None) -> str:
        path = f"{stage_id}/records/{record.id}-start.json"
        try:
            data = self.store.get_blob(path)
        except ResourceNotFoundError:
            data = canonical_json({"started_at": ready_at or utc_now()}).encode()
            try:
                self.store.put_blob(path, data)
            except ResourceExistsError:
                data = self.store.get_blob(path)
        return str(json.loads(data)["started_at"])

    def record_state(self, item: dict[str, Any]) -> dict[str, Any]:
        root, arm, index = item["run_id"], item["arm"], item["index"]
        stage_id, record = self._stage_id(root, arm), self._record(root, index)
        existing = self.store.get_item("records", stage_id, record.id)
        if existing:
            return {"terminal": True, "status": existing["status"]}
        self._start_record(stage_id, record, item.get("ready_at"))
        if self.store.get_item("control", root, "cancel"):
            recovered = self.saved_attempt(item)
            if recovered and recovered["terminal"]:
                return recovered
            return self.finish_record({**item, "error": "cancelled_before_attempt"})
        return {"terminal": False}

    def saved_attempt(self, item: dict[str, Any]) -> dict[str, Any] | None:
        root, arm, index = item["run_id"], item["arm"], item["index"]
        record = self._record(root, index)
        attempt = item["attempt"]
        spec = self.spec_model.model_validate(self._manifest(root)["specification"])
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 1 <= attempt <= spec.max_attempts
        ):
            raise ValueError("Attempt exceeds the per-record retry budget")
        prefix = f"{self._stage_id(root, arm)}/attempts/{record.id}-try-{attempt}"
        try:
            bundle = json.loads(self.store.get_blob(prefix + "-response.json"))
        except ResourceNotFoundError:
            try:
                self.store.get_blob(prefix + "-intent.json")
            except ResourceNotFoundError:
                return None
            return self.finish_record({**item, "error": "ambiguous_online_result"})
        return self._commit_attempt({"target": bundle["target_alias"], **item}, bundle)

    def finish_record(self, item: dict[str, Any]) -> dict[str, Any]:
        root, arm, index = item["run_id"], item["arm"], item["index"]
        stage_id, record = self._stage_id(root, arm), self._record(root, index)
        existing = self.store.get_item("records", stage_id, record.id)
        if existing:
            return {"terminal": True, "status": existing["status"]}
        outcome: dict[str, Any] = {
            "status": "failed",
            "error": item["error"],
            "retryable": False,
            "started_at": self._start_record(stage_id, record, item.get("ready_at")),
            "ended_at": utc_now(),
        }
        outcome.update(self.timing_fields(stage_id, outcome["started_at"], outcome["ended_at"]))
        self._record_wait(stage_id, record.id, item.get("router_wait_seconds", 0))
        annotate_result(record, outcome)
        self.store.save("records", stage_id, record.id, outcome)
        return {"terminal": True, "status": outcome["status"]}

    def _record_wait(self, stage_id: str, record_id: str, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("Invalid v4 wait duration")
        wait = event("router_wait", record_id=record_id, duration_seconds=seconds)
        wait["event_id"] = f"{record_id}-router-wait"
        self.store.add_event(stage_id, wait)

    def attempt(self, item: dict[str, Any]) -> dict[str, Any]:
        root, arm, index = item["run_id"], item["arm"], item["index"]
        stage_id, record = self._stage_id(root, arm), self._record(root, index)
        state = self.record_state(item)
        if state["terminal"]:
            return state
        manifest = self._manifest(root)
        spec = self.spec_model.model_validate(manifest["specification"])
        attempt = item["attempt"]
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 1 <= attempt <= spec.max_attempts
        ):
            raise ValueError("V4 attempt exceeds the per-record retry budget")
        target = self.target_map(arm).get(item["target"])
        if target is None:
            raise ValueError("Target is not in this arm's approved deployment map")
        key = f"{record.id}-try-{attempt}"
        response_path = f"{stage_id}/attempts/{key}-response.json"
        recovered = self.saved_attempt(item)
        if recovered is not None:
            return recovered
        if elapsed_between(self.store.get_run(stage_id)["started_at"]) >= self.arm_timeout(
            spec, arm
        ):
            return self.finish_record({**item, "error": "admission_deadline"})
        try:
            self.store.put_blob(
                f"{stage_id}/attempts/{key}-intent.json",
                canonical_json(
                    {
                        "record_id": record.id,
                        "attempt": attempt,
                        "target_alias": item["target"],
                        "at": utc_now(),
                    }
                ).encode(),
            )
        except ResourceExistsError:
            return self.finish_record({**item, "error": "ambiguous_online_result"})
        settings = Settings.model_validate(
            {**self.settings.model_dump(), "online_deployment": target["deployment"]}
        )
        provider = self.provider_for(settings)
        started = time.monotonic()
        requested = utc_now()
        bundle = {
            "target_alias": item["target"],
            "attempt": attempt,
            "requested_at": requested,
            "estimated_input_tokens": manifest["estimated_tokens"][index],
            "router_wait_seconds": item.get("router_wait_seconds", 0),
        }
        try:
            body, headers = provider.complete(record)
            bundle.update(status_code=200, body=body, headers=rate_headers(headers))
        except openai.APIStatusError as exc:
            error = safe_error(exc)
            bundle.update(
                status_code=error["status_code"],
                error=error["type"],
                headers=rate_headers(error["headers"]),
                error_indication=error_indication(exc.body),
            )
        except openai.APIConnectionError as exc:
            bundle.update(
                status_code=None,
                error="ambiguous_online_result",
                transport_error=type(exc).__name__,
                headers={},
            )
        finally:
            self.release_provider(provider)
        bundle.update(completed_at=utc_now(), duration_seconds=time.monotonic() - started)
        status = bundle["status_code"]
        bundle["retry_delay_seconds"] = (
            retry_seconds(bundle["headers"], attempt)
            if status == 429 or (isinstance(status, int) and status >= 500)
            else 0
        )
        self._put_once(response_path, json.dumps(bundle, separators=(",", ":")).encode())
        return self._commit_attempt(item, bundle)

    def _commit_attempt(self, item: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
        root, arm, index = item["run_id"], item["arm"], item["index"]
        stage_id, record = self._stage_id(root, arm), self._record(root, index)
        if bundle["target_alias"] != item["target"] or bundle["attempt"] != item["attempt"]:
            raise ValueError("Persisted v4 response belongs to a different attempt/target")
        spec = self.spec_model.model_validate(self._manifest(root)["specification"])
        status = bundle["status_code"]
        body = bundle.get("body") or {}
        item_event = event(
            "http_attempt",
            duration_seconds=bundle["duration_seconds"],
            record_id=record.id,
            status_code=status,
            attempt=item["attempt"],
            target_alias=item["target"],
            requested_at=bundle["requested_at"],
            headers=bundle["headers"],
            usage=body.get("usage") or {},
            error=bundle.get("error"),
            error_indication=bundle.get("error_indication"),
            observed_model=body.get("model"),
        )
        item_event.update(event_id=f"{record.id}-try-{item['attempt']}", at=bundle["completed_at"])
        self.store.add_event(stage_id, item_event)
        self._record_wait(stage_id, record.id, bundle.get("router_wait_seconds", 0))
        retryable = status == 429 or isinstance(status, int) and status >= 500
        fatal = status in {400, 401, 403, 404, 422}
        model = body.get("model")
        if status == 200 and model not in {"gpt-5-mini", "gpt-5-mini-2025-08-07"}:
            fatal, retryable = True, False
            outcome = {"status": "failed", "error": "unexpected_model_response", "retryable": False}
        elif status == 200:
            outcome = validate_completion(body, self.settings.output_schema)
        else:
            outcome = {
                "status": "failed",
                "error": bundle["error"],
                "status_code": status,
                "retryable": retryable,
            }
        terminal = not retryable or item["attempt"] >= spec.max_attempts
        if fatal:
            self.store.save(
                "control",
                root,
                "cancel",
                {"requested": True, "reason": "fatal_model_or_request_error"},
            )
        if terminal:
            start = self._start_record(stage_id, record)
            outcome.update(
                **self.timing_fields(stage_id, start, bundle["completed_at"]),
                attempts=item["attempt"],
                target_alias=item["target"],
            )
            annotate_result(record, outcome)
            self.store.save("records", stage_id, record.id, outcome)
        return {
            "terminal": terminal,
            "status": outcome["status"],
            "status_code": status,
            "retryable": retryable,
            "retry_after": bundle["retry_delay_seconds"],
            "completed_at": bundle["completed_at"],
            "http_duration_seconds": bundle["duration_seconds"],
            "budget_headers": {
                key: value
                for key, value in bundle["headers"].items()
                if key.startswith("x-ratelimit-")
            },
        }

    def finalize_arm(self, root: str, arm: str) -> dict[str, Any]:
        stage_id = self._stage_id(root, arm)
        run = self.store.get_run(stage_id)
        records, events = (
            self.store.items("records", stage_id),
            self.store.items("events", stage_id),
        )
        if len(records) != run["record_count"]:
            raise ValueError("V4 cannot finalize an arm with unreconciled records")
        run.update(
            status="succeeded"
            if all(row["status"] == "succeeded" for row in records)
            else "partial_failure",
            ended_at=run.get("ended_at") or utc_now(),
        )
        report = summarize_documents(run, records, events, [])
        report["target_metrics"] = []
        for target in self.public_target_map(arm):
            attempts = [
                e
                for e in events
                if e.get("target_alias") == target["alias"] and e["stage"] == "http_attempt"
            ]
            report["target_metrics"].append(
                {
                    **target,
                    "http_attempts": len(attempts),
                    "responses_200": sum(e["status_code"] == 200 for e in attempts),
                    "errors_429": sum(e["status_code"] == 429 for e in attempts),
                    "http_200_latency_mean_seconds": mean(
                        [e["duration_seconds"] for e in attempts if e["status_code"] == 200]
                    ),
                    "http_200_latency_p95_seconds": percentile(
                        [e["duration_seconds"] for e in attempts if e["status_code"] == 200], 0.95
                    ),
                    "observed_rate_limits": sorted(
                        {
                            (
                                e["headers"].get("x-ratelimit-limit-tokens", ""),
                                e["headers"].get("x-ratelimit-limit-requests", ""),
                            )
                            for e in attempts
                            if e["headers"].get("x-ratelimit-limit-tokens")
                        }
                    ),
                    "error_indications": dict(
                        Counter(
                            e.get("error_indication", "unspecified")
                            for e in attempts
                            if e["status_code"] == 429
                        )
                    ),
                }
            )
        spec = self.spec_model.model_validate(self._manifest(root)["specification"])
        report["gate"] = quality_gate(
            report,
            Scenario(
                name=self.profile["experiment"],
                count=self.arm_count(spec, arm),
                stages=[Stage(name="online", mode="parallel")],
            ),
        )
        self.store.put_blob(
            f"{stage_id}/summary.json", canonical_json(report).encode(), overwrite=True
        )
        self.store.put_blob(
            f"{stage_id}/results.jsonl",
            ("\n".join(canonical_json(row) for row in records) + "\n").encode(),
            overwrite=True,
        )
        self.store.put_blob(
            f"{stage_id}/analysis.csv", analysis_csv(records).encode(), overwrite=True
        )
        run["summary"] = {
            key: report[key]
            for key in (
                "succeeded",
                "failed",
                "pending",
                "elapsed_seconds",
                "errors_429",
                "semantic_status",
            )
        }
        self.store.save_run(run)
        return {"arm": arm, "report": report}

    def finalize(self, root: str) -> dict[str, Any]:
        run = self.store.get_run(root)
        all_records, all_events, reports = [], [], []
        samples = []
        for arm in self.comparison_arms:
            stage_id = self._stage_id(root, arm)
            report = json.loads(self.store.get_blob(f"{stage_id}/summary.json"))
            reports.append(report)
            records = self.store.items("records", stage_id)
            all_records += [{**row, "id": f"{arm}-{row['id']}"} for row in records]
            all_events += [
                {**e, "record_id": f"{arm}-{e.get('record_id', '')}"}
                for e in self.store.items("events", stage_id)
            ]
            samples.append(
                {
                    "arm": arm,
                    "records": [
                        {
                            key: row.get(key)
                            for key in ("id", "status", "error", "result", "quality")
                        }
                        for row in sorted(records, key=lambda row: row["id"])[:3]
                    ],
                }
            )
        run.update(
            status="succeeded"
            if all(r["status"] == "succeeded" for r in reports)
            else "needs_attention",
            ended_at=run.get("ended_at") or utc_now(),
        )
        summary = summarize_documents(run, all_records, all_events, [])
        manifest = self._manifest(root)
        summary.update(
            multi_deployment_stages=reports,
            specification=manifest["specification"],
            conservative_all_attempts_estimate_usd=manifest["plan"][
                "conservative_all_attempts_estimate_usd"
            ],
            acceptance_status="passed"
            if all(r["gate"]["status"] == "passed" for r in reports)
            else "needs_review",
            synthetic_output_samples=samples,
            router_reconciliation_required=any(
                row.get("error") == "ambiguous_online_result" for row in all_records
            ),
        )
        summary.update(self.final_extra(root, summary))
        self.store.put_blob(
            f"{root}/summary.json", canonical_json(summary).encode(), overwrite=True
        )
        self.store.put_blob(
            f"{root}/results.jsonl",
            ("\n".join(canonical_json(row) for row in all_records) + "\n").encode(),
            overwrite=True,
        )
        self.store.put_blob(
            f"{root}/results.csv", results_csv(all_records).encode(), overwrite=True
        )
        self.store.put_blob(
            f"{root}/analysis.csv", analysis_csv(all_records).encode(), overwrite=True
        )
        run["summary"] = {
            key: summary[key]
            for key in (
                "succeeded",
                "failed",
                "pending",
                "elapsed_seconds",
                "errors_429",
                "semantic_status",
                "acceptance_status",
            )
        }
        self.store.save_run(run)
        return {
            "run_id": root,
            "status": run["status"],
            **run["summary"],
            "router_reconciliation_required": summary["router_reconciliation_required"],
            "report": summary,
        }
