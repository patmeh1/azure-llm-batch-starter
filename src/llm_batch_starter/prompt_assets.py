from __future__ import annotations

from importlib import resources
from pathlib import Path


def read_prompt(name: str) -> str:
    if Path(name).name != name or not name.endswith(".md"):
        raise ValueError("Prompt asset must be a Markdown filename")
    packaged = resources.files("llm_batch_starter").joinpath("prompts", name)
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8").strip()
    source = Path(__file__).resolve().parents[2] / "prompts" / name
    if source.is_file():
        return source.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(f"Missing packaged prompt asset: {name}")
