import argparse
import json
import time
from pathlib import Path

from llm_batch_starter.complex_workload import generate_complex
from llm_batch_starter.config import load_settings
from llm_batch_starter.pipeline import run_pipeline
from llm_batch_starter.synthetic import generate_dataset
from llm_batch_starter.workloads import configure_profile

parser = argparse.ArgumentParser(description="Same-data sequential and parallel online comparison.")
parser.add_argument("--config", type=Path, required=True)
parser.add_argument("--count", type=int, default=100)
parser.add_argument("--concurrency", type=int, default=5)
parser.add_argument("--rpm", type=int)
parser.add_argument("--profile", choices=["simple", "complex-v2"], default="simple")
parser.add_argument("--cooldown-seconds", type=int, default=65)
parser.add_argument("--yes", action="store_true")
args = parser.parse_args()
if not args.yes or not 1 <= args.count <= 1700 or not 0 <= args.cooldown_seconds <= 3600:
    raise SystemExit("Review charges, use 1..1700 records, and pass --yes.")
records = (
    generate_complex(args.count, seed=42)
    if args.profile == "complex-v2"
    else generate_dataset(args.count, seed=42)
)
settings, metadata = configure_profile(
    load_settings(args.config, concurrency=args.concurrency, rpm=args.rpm), args.profile
)
summaries = []
for mode in ("sequential", "parallel"):
    if summaries:
        time.sleep(args.cooldown_seconds)
    report = run_pipeline(
        records,
        settings,
        mode,
        Path("outputs/state.sqlite3"),
        Path("outputs/runs"),
        workload_profile={
            **metadata,
            "experiment": "online-comparison",
            "client_rpm": settings.rpm,
        },
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
                "quality",
                "semantic_status",
            )
        }
    )
    print(json.dumps(summaries[-1]), flush=True)
prefix = "" if args.profile == "simple" else f"{args.profile}-"
Path(f"outputs/online-comparison-{prefix}{settings.rpm}rpm.json").write_text(
    json.dumps(summaries, indent=2), encoding="utf-8"
)
raise SystemExit(0 if all(row["status"] == "succeeded" for row in summaries) else 1)
