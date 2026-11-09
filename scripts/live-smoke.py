from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from llm_batch_starter.config import load_settings
from llm_batch_starter.load_testing import Scenario, quality_gate
from llm_batch_starter.scheduler import SchedulerAccess
from llm_batch_starter.workloads import configure_profile

parser = argparse.ArgumentParser()
parser.add_argument("--config", type=Path)
parser.add_argument("--count", type=int)
parser.add_argument("--profile", choices=["simple", "complex-v2"], default="simple")
parser.add_argument("--timeout", type=int, default=900)
parser.add_argument("--yes", action="store_true")
args = parser.parse_args()
if not args.yes:
    raise SystemExit("Paid test: pass --yes only after reviewing costs and deployment context.")
count = args.count if args.count is not None else (13 if args.profile == "complex-v2" else 10)
settings, _ = configure_profile(load_settings(args.config), args.profile)
store = SchedulerAccess(settings)
try:
    run_id = (
        store.start_complex(count, seed=42)
        if args.profile == "complex-v2"
        else store.start_synthetic(count, seed=42)
    )
    print(json.dumps({"run_id": run_id, "profile": args.profile, "status": "queued"}), flush=True)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        current = store.get_run(run_id)
        if current["status"] not in {"queued", "running", "pending"}:
            report = store.detail(run_id)
            gate = quality_gate(
                report,
                Scenario(
                    name="cloud-smoke",
                    profile=args.profile,
                    count=count,
                    stages=[{"name": "batch", "mode": "batch"}],
                ),
            )
            print(
                json.dumps({"status": current["status"], "gate": gate, "summary": report}),
                flush=True,
            )
            raise SystemExit(0 if gate["status"] == "passed" else 1)
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
