from pathlib import Path


def test_shell_scripts_use_lf_even_on_windows():
    root = Path(__file__).parents[1] / "scripts"
    for name in ("demo.sh", "preflight.sh", "package-functions.sh"):
        data = (root / name).read_bytes()
        assert data.startswith(b"#!/usr/bin/env sh\n"), name
        assert b"\r\n" not in data, name
