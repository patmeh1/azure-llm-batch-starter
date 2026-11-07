from hashlib import sha256
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = root / "src" / "llm_batch_starter"
target = root / "services" / "orchestrator" / "llm_batch_starter"
missing_or_stale = []
for path in source.glob("*.py"):
    copied = target / path.name
    if (
        not copied.exists()
        or sha256(path.read_bytes()).digest() != sha256(copied.read_bytes()).digest()
    ):
        missing_or_stale.append(path.name)
if missing_or_stale:
    raise SystemExit("Missing/stale deployed source files: " + ", ".join(missing_or_stale))
print("All shared Python modules match the deployment package.")
