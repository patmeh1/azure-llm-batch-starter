import json
import os
from pathlib import Path

required = ["AZURE_OPENAI_ENDPOINT", "AZURE_STORAGE_ACCOUNT", "AZURE_LOCATION"]
missing = [key for key in required if not os.environ.get(key)]
if missing:
    raise SystemExit("Deployment outputs missing: " + ", ".join(missing))
path = Path(".azure") / "demo-config.json"
path.parent.mkdir(exist_ok=True)
path.write_text(
    json.dumps(
        {
            "endpoint": os.environ["AZURE_OPENAI_ENDPOINT"],
            "storage_account": os.environ["AZURE_STORAGE_ACCOUNT"],
            "region": os.environ["AZURE_LOCATION"],
            "online_deployment": os.environ.get("AZURE_OPENAI_ONLINE_DEPLOYMENT", "online"),
            "batch_deployment": os.environ.get("AZURE_OPENAI_BATCH_DEPLOYMENT", "batch"),
            "auth_mode": "cli",
            "scheduler_endpoint": os.environ.get("DTS_ENDPOINT", ""),
            "task_hub": os.environ.get("TASKHUB_NAME", ""),
            "model_name": os.environ.get("AZURE_OPENAI_MODEL_NAME", "gpt-5.4-mini"),
            "model_version": os.environ.get("AZURE_OPENAI_MODEL_VERSION", "2026-03-17"),
            "max_run_cost_usd": 25,
        },
        indent=2,
    ),
    encoding="utf-8",
)
print("Private local configuration written to .azure/demo-config.json (ignored by Git).")
