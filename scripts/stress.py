from __future__ import annotations

import argparse
import json
from pathlib import Path

from llm_batch_starter.config import load_settings
from llm_batch_starter.pipeline import run_pipeline
from llm_batch_starter.prepare import estimate_input_tokens, synthetic_target_padding
from llm_batch_starter.synthetic import generate_dataset

parser = argparse.ArgumentParser(
    description="Bounded synthetic input-token/concurrency experiment."
)
parser.add_argument("--config", type=Path, required=True)
parser.add_argument("--baseline-tokens", type=int, required=True)
parser.add_argument("--multiplier", type=float, default=2)
parser.add_argument("--count", type=int, default=6)
parser.add_argument("--concurrency", type=int, choices=[1, 2, 5], default=2)
parser.add_argument("--observe-service-throttling", action="store_true")
parser.add_argument("--vary-prefix", action="store_true")
parser.add_argument("--yes", action="store_true")
args = parser.parse_args()
if not args.yes:
    raise SystemExit("Paid experiment: review the configuration and pass --yes to run.")
if not 1 <= args.count <= 30 or args.baseline_tokens < 1 or not 1 <= args.multiplier <= 4:
    raise SystemExit("Use 1..30 records, a positive baseline, and a multiplier in 1..4.")
settings = load_settings(
    args.config,
    concurrency=args.concurrency,
    max_attempts=1,
    max_http_attempts=30,
    run_timeout_seconds=180,
    observe_service_throttling=args.observe_service_throttling,
)
target = round(args.baseline_tokens * args.multiplier)
padding = synthetic_target_padding(target, settings)
records = generate_dataset(args.count, reference_tokens=padding)
if args.vary_prefix:
    records = [
        record.model_copy(update={"text": f"SYNTHETIC CASE {record.id}\n{record.text}"})
        for record in records
    ]
result = run_pipeline(
    records,
    settings,
    "sequential" if args.concurrency == 1 else "parallel",
    Path("outputs/state.sqlite3"),
    Path("outputs/runs"),
    workload_profile={
        "name": "bounded-token-stress",
        "baseline_tokens": args.baseline_tokens,
        "multiplier": args.multiplier,
        "target_input_tokens": target,
        "estimated_input_token_range": [
            min(estimate_input_tokens(record, settings) for record in records),
            max(estimate_input_tokens(record, settings) for record in records),
        ],
        "prefix_profile": "varied" if args.vary_prefix else "stable",
    },
)
print(
    json.dumps(
        {
            "experiment_completed": True,
            "run_id": result["id"],
            "workload_status": result["status"],
            "target_input_tokens": target,
            "prefix_profile": "varied" if args.vary_prefix else "stable",
            "succeeded": result["succeeded"],
            "failed": result["failed"],
            "errors_429": result["errors_429"],
            "elapsed_seconds": result["elapsed_seconds"],
            "note": "Intentional stress may have failed records; experiment completion is not inference success.",
        },
        indent=2,
    )
)
