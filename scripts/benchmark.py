import argparse
import json
from pathlib import Path

from llm_batch_starter.config import load_settings
from llm_batch_starter.pipeline import run_pipeline
from llm_batch_starter.synthetic import generate_dataset

parser = argparse.ArgumentParser(description="Same-data sequential and parallel online comparison.")
parser.add_argument("--config", type=Path, required=True)
parser.add_argument("--count", type=int, default=100)
parser.add_argument("--concurrency", type=int, default=5)
parser.add_argument("--rpm", type=int)
parser.add_argument("--yes", action="store_true")
args = parser.parse_args()
if not args.yes or not 1 <= args.count <= 1700:
    raise SystemExit("Review charges, use 1..1700 records, and pass --yes.")
records = generate_dataset(args.count, seed=42)
settings = load_settings(args.config, concurrency=args.concurrency, rpm=args.rpm)
summaries = []
for mode in ("sequential", "parallel"):
    report = run_pipeline(
        records,
        settings,
        mode,
        Path("outputs/state.sqlite3"),
        Path("outputs/runs"),
        workload_profile={"name": "compact-online-comparison", "client_rpm": settings.rpm},
    )
    summaries.append(
        {
            key: report.get(key)
            for key in (
                "id",
                "mode",
                "status",
                "record_count",
                "succeeded",
                "failed",
                "pending",
                "elapsed_seconds",
                "completed_records_per_minute",
                "errors_429",
                "http_200_latency_p50_seconds",
                "http_200_latency_p95_seconds",
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "priced_usage_estimate_usd",
                "label_accuracy",
            )
        }
    )
    print(json.dumps(summaries[-1]), flush=True)
Path(f"outputs/online-comparison-{settings.rpm}rpm.json").write_text(
    json.dumps(summaries, indent=2), encoding="utf-8"
)
raise SystemExit(0 if all(row["status"] == "succeeded" for row in summaries) else 1)
