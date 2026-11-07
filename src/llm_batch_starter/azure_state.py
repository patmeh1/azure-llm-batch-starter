from __future__ import annotations

import json
import re
import uuid
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient, TextBase64EncodePolicy

from .config import Settings, credential
from .prepare import estimate_cost, input_fingerprint, prepare_requests
from .synthetic import Record
from .telemetry import canonical_json, utc_now


def valid_run_id(run_id: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", run_id):
        raise ValueError("Invalid run ID")
    return run_id


class AzureState:
    def __init__(self, settings: Settings):
        if not re.fullmatch(r"[a-z0-9]{3,24}", settings.storage_account):
            raise ValueError("Set AZURE_STORAGE_ACCOUNT to the deployed account name")
        self.settings = settings
        self.identity = credential(settings)
        account = settings.storage_account
        self.blobs = BlobServiceClient(
            f"https://{account}.blob.core.windows.net", credential=self.identity
        )
        self.table = TableClient(
            f"https://{account}.table.core.windows.net",
            table_name=settings.table_name,
            credential=self.identity,
        )
        self.queue = QueueClient(
            f"https://{account}.queue.core.windows.net",
            queue_name=settings.queue_name,
            credential=self.identity,
            message_encode_policy=TextBase64EncodePolicy(),
        )

    def close(self) -> None:
        self.blobs.close()
        self.table.close()
        self.queue.close()
        self.identity.close()

    def put_blob(self, name: str, data: bytes, *, overwrite: bool = False) -> None:
        self.blobs.get_blob_client(self.settings.container, name).upload_blob(
            data, overwrite=overwrite
        )

    def get_blob(self, name: str) -> bytes:
        return self.blobs.get_blob_client(self.settings.container, name).download_blob().readall()

    def save(self, table: str, run_id: str, item_id: str, document: dict[str, Any]) -> None:
        valid_run_id(run_id)
        if table not in {"records", "jobs", "events", "control"}:
            raise ValueError("Invalid metadata kind")
        self.table.upsert_entity(
            {
                "PartitionKey": run_id,
                "RowKey": f"{table}:{item_id}",
                "document": canonical_json(document),
            },
            mode=UpdateMode.REPLACE,
        )

    def save_run(self, document: dict[str, Any], *, create: bool = False) -> None:
        entity = {
            "PartitionKey": valid_run_id(document["id"]),
            "RowKey": "run",
            "document": canonical_json(document),
        }
        if create:
            self.table.create_entity(entity)
        else:
            self.table.upsert_entity(entity, mode=UpdateMode.REPLACE)

    def get_run(self, run_id: str) -> dict[str, Any]:
        value = self.table.get_entity(valid_run_id(run_id), "run")
        return dict(json.loads(value["document"]))

    def list_runs(self) -> list[dict[str, Any]]:
        return [
            json.loads(item["document"]) for item in self.table.query_entities("RowKey eq 'run'")
        ]

    def items(self, table: str, run_id: str) -> list[dict[str, Any]]:
        valid_run_id(run_id)
        return [
            json.loads(item["document"])
            for item in self.table.query_entities(
                "PartitionKey eq @run", parameters={"run": run_id}
            )
            if item["RowKey"].startswith(table + ":")
        ]

    def get_item(self, table: str, run_id: str, item_id: str) -> dict[str, Any] | None:
        try:
            value = self.table.get_entity(valid_run_id(run_id), f"{table}:{item_id}")
        except ResourceNotFoundError:
            return None
        return dict(json.loads(value["document"]))

    def add_event(self, run_id: str, item: dict[str, Any]) -> None:
        self.save("events", run_id, item["event_id"], item)

    def enqueue(self, run_id: str, action: str = "start") -> None:
        if action not in {"start", "cancel"}:
            raise ValueError("Invalid cloud action")
        self.queue.send_message(canonical_json({"run_id": valid_run_id(run_id), "action": action}))


def create_cloud_run(
    records: list[Record],
    settings: Settings,
    *,
    run_id: str | None = None,
    enqueue: bool = True,
    parent_run_id: str | None = None,
) -> dict[str, Any]:
    prepare_requests(records, settings)
    estimate = estimate_cost(records, settings, batch=True)
    if estimate["estimated_inference_usd"] > settings.max_run_cost_usd:
        raise ValueError("Estimated inference exceeds the configured per-run limit")
    run_id = valid_run_id(run_id or uuid.uuid4().hex)
    store = AzureState(settings)
    try:
        fingerprint = input_fingerprint(records, settings)
        try:
            existing = store.get_run(run_id)
        except ResourceNotFoundError:
            existing = None
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise ValueError("Existing cloud run has different inputs or configuration")
            return existing
        config = settings.model_dump(
            exclude={
                "endpoint",
                "auth_mode",
                "managed_identity_client_id",
                "storage_account",
                "coordination_dir",
                "scheduler_endpoint",
                "task_hub",
            }
        )
        manifest = {"records": [r.model_dump() for r in records], "settings": config}
        # Schema property order affects structured-output order and the cached prompt prefix.
        payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
        if len(payload) > 50_000_000:
            raise ValueError("Cloud manifest exceeds the starter's 50 MB limit; split the run")
        try:
            store.put_blob(f"{run_id}/manifest.json", payload)
        except ResourceExistsError:
            if store.get_blob(f"{run_id}/manifest.json") != payload:
                raise ValueError("Existing manifest differs; do not overwrite it") from None
        run = {
            "id": run_id,
            "mode": "batch",
            "status": "queued",
            "started_at": utc_now(),
            "ended_at": None,
            "record_count": len(records),
            "source": "live-azure",
            "metadata": settings.public_metadata(),
            "estimate": estimate,
            "fingerprint": fingerprint,
            "parent_run_id": parent_run_id,
        }
        store.save_run(run, create=True)
        try:
            if enqueue:
                store.enqueue(run_id)
        except ResourceExistsError:
            raise RuntimeError(
                "Unexpected queue conflict; inspect the saved run before retrying"
            ) from None
        return run
    finally:
        store.close()
