import runpy
import shutil
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = root / "src" / "llm_batch_starter"
target = root / "services" / "orchestrator" / "llm_batch_starter"
if target.exists():
    shutil.rmtree(target)
shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
shutil.copytree(root / "prompts", target / "prompts")
runpy.run_path(str(root / "scripts" / "verify-shared-package.py"), run_name="__main__")
print("Packaged the shared application into the Functions deployment directory.")
