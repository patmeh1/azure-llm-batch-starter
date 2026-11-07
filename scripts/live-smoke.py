from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from llm_batch_starter.config import load_settings
from llm_batch_starter.scheduler import SchedulerAccess

parser = argparse.ArgumentParser()
parser.add_argument("--config", type=Path)
parser.add_argument("--count", type=int, default=10)
parser.add_argument("--timeout", type=int, default=900)
parser.add_argument("--yes", action="store_true")
args = parser.parse_args()
if not args.yes:
    raise SystemExit("Paid test: pass --yes only after reviewing costs and deployment context.")
settings = load_settings(args.config)
store = SchedulerAccess(settings)
run_id = store.start_synthetic(args.count, seed=42)
print(json.dumps({"run_id": run_id, "status": "queued"}), flush=True)
deadline = time.monotonic() + args.timeout
try:
    while time.monotonic() < deadline:
        current = store.get_run(run_id)
        if current["status"] not in {"queued", "running", "pending"}:
            print(
                json.dumps({"status": current["status"], "summary": current.get("summary")}),
                flush=True,
            )
            raise SystemExit(0 if current["status"] == "succeeded" else 1)
        time.sleep(15)
    print(
        json.dumps(
            {
                "status": "pending",
                "run_id": run_id,
                "note": "Monitoring deadline reached; cloud job continues.",
            }
        ),
        flush=True,
    )
    raise SystemExit(2)
finally:
    store.close()
