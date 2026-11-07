from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

root = Path(__file__).resolve().parents[1]
reports = root / "reports"
reports.mkdir(exist_ok=True)
stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
xml = reports / f"tests-{stamp}.xml"
started = time.perf_counter()
arguments = sys.argv[1:] or ["-m", "not live and not browser and not emulator"]
result = subprocess.run(
    [sys.executable, "-m", "pytest", "--durations=20", f"--junitxml={xml}", *arguments],
    cwd=root,
    check=False,
)
tests = []
if xml.exists():
    for case in ET.parse(xml).iter("testcase"):
        status = "passed"
        for kind in ("failure", "error", "skipped"):
            if case.find(kind) is not None:
                status = kind
                break
        tests.append(
            {
                "name": f"{case.get('classname', '')}.{case.get('name', '')}",
                "status": status,
                "duration_seconds": float(case.get("time", "0")),
            }
        )
summary = {
    "timestamp": datetime.now(UTC).isoformat(),
    "status": "passed" if result.returncode == 0 else "failed",
    "exit_code": result.returncode,
    "duration_seconds": time.perf_counter() - started,
    "platform": platform.system(),
    "python": platform.python_version(),
    "tests": tests,
}
(reports / f"test-summary-{stamp}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
raise SystemExit(result.returncode)
