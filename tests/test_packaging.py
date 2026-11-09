import os
import shutil
import subprocess
from pathlib import Path


def test_shell_scripts_use_lf_even_on_windows():
    root = Path(__file__).parents[1] / "scripts"
    for name in ("demo.sh", "preflight.sh", "package-functions.sh"):
        data = (root / name).read_bytes()
        assert data.startswith(b"#!/usr/bin/env sh\n"), name
        assert b"\r\n" not in data, name


def test_native_packaging_hook_copies_markdown_and_python(tmp_path):
    root = Path(__file__).parents[1]
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (
        "package-functions.ps1",
        "package-functions.sh",
        "package-functions.py",
        "verify-shared-package.py",
    ):
        shutil.copyfile(root / "scripts" / name, scripts / name)
    source = tmp_path / "src" / "llm_batch_starter"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("# test package\n", encoding="utf-8")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "test.md").write_text("Active prompt instructions.\n", encoding="utf-8")
    (tmp_path / "services" / "orchestrator").mkdir(parents=True)
    command = (
        ["pwsh", "-NoProfile", "-File", str(scripts / "package-functions.ps1")]
        if os.name == "nt"
        else ["sh", str(scripts / "package-functions.sh")]
    )
    subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, text=True)
    target = tmp_path / "services" / "orchestrator" / "llm_batch_starter"
    assert (target / "__init__.py").read_bytes() == (source / "__init__.py").read_bytes()
    assert (target / "prompts" / "test.md").read_bytes() == (prompts / "test.md").read_bytes()
