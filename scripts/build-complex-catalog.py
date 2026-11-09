from pathlib import Path

from llm_batch_starter.complex_workload import render_catalog

target = Path(__file__).resolve().parents[1] / "prompts" / "complex-taxonomy-v2.md"
target.write_text(render_catalog(), encoding="utf-8", newline="\n")
print(f"Wrote {target.name}: 425 synthetic routes and 850 examples.")
