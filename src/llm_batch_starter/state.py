from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .telemetry import canonical_json, utc_now


class State:
    def __init__(self, path: Path, *, readonly: bool = False):
        self.path = path
        self.lock = threading.RLock()
        if readonly:
            self.connection = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (
                    run_id TEXT NOT NULL, id TEXT NOT NULL, document TEXT NOT NULL,
                    PRIMARY KEY (run_id,id));
                CREATE TABLE IF NOT EXISTS jobs (
                    run_id TEXT NOT NULL, id TEXT NOT NULL, document TEXT NOT NULL,
                    PRIMARY KEY (run_id,id));
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL, id TEXT NOT NULL, document TEXT NOT NULL,
                    PRIMARY KEY (run_id,id));
                """
            )
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()

    def save_run(self, document: dict[str, Any]) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO runs(id,document) VALUES(?,?) "
                "ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                (document["id"], canonical_json(document)),
            )

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            row = self.connection.execute(
                "SELECT document FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        if not row:
            raise ValueError(f"Unknown run: {run_id}")
        return dict(json.loads(row["document"]))

    def list_runs(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT document FROM runs ORDER BY rowid DESC"
            ).fetchall()
        return [json.loads(row["document"]) for row in rows]

    def save(self, table: str, run_id: str, item_id: str, document: dict[str, Any]) -> None:
        if table not in {"records", "jobs", "events"}:
            raise ValueError("Invalid state table")
        with self.lock, self.connection:
            self.connection.execute(
                f"INSERT INTO {table}(run_id,id,document) VALUES(?,?,?) "
                "ON CONFLICT(run_id,id) DO UPDATE SET document=excluded.document",
                (run_id, item_id, canonical_json(document)),
            )

    def items(self, table: str, run_id: str) -> list[dict[str, Any]]:
        if table not in {"records", "jobs", "events"}:
            raise ValueError("Invalid state table")
        with self.lock:
            rows = self.connection.execute(
                f"SELECT document FROM {table} WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [json.loads(row["document"]) for row in rows]

    def add_event(self, run_id: str, item: dict[str, Any]) -> None:
        self.save("events", run_id, item["event_id"], item)

    def finish(self, run_id: str, status: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        run.update(status=status, ended_at=utc_now())
        self.save_run(run)
        return run
