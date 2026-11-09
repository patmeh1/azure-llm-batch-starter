import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--category", choices=["browser", "site"], default="browser")
args = parser.parse_args()
source = Path(f"reports/{args.category}-results.json")
if not source.exists():
    raise SystemExit(f"No {args.category} result file; run npm run test:{args.category} first.")
report = json.loads(source.read_text(encoding="utf-8"))
tests = []


def visit(suite):
    for spec in suite.get("specs", []):
        for case in spec.get("tests", []):
            results = case.get("results", [])
            tests.append(
                {
                    "name": spec["title"],
                    "status": results[-1]["status"] if results else "not_run",
                    "duration_seconds": sum(row["duration"] for row in results) / 1000,
                }
            )
    for child in suite.get("suites", []):
        visit(child)


for suite in report.get("suites", []):
    visit(suite)
stamp = datetime.now(UTC)
summary = {
    "timestamp": stamp.isoformat(),
    "status": "passed" if tests and all(t["status"] == "passed" for t in tests) else "failed",
    "duration_seconds": report.get("stats", {}).get("duration", 0) / 1000,
    "category": args.category,
    "tests": tests,
}
Path(f"reports/test-summary-{args.category}-{stamp:%Y%m%dT%H%M%SZ}.json").write_text(
    json.dumps(summary, indent=2), encoding="utf-8"
)
