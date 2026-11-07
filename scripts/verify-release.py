from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from llm_batch_starter import __version__

stem = f"azure_llm_batch_starter-{__version__}"
shell_scripts = set()
with tarfile.open(Path("dist") / f"{stem}.tar.gz") as archive:
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if any(
            part in {".azure", "outputs", "reports", "node_modules", ".git"} for part in path.parts
        ):
            raise ValueError(f"Private/generated directory in source archive: {member.name}")
        if path.name.startswith(".env") and path.name != ".env.example":
            raise ValueError(f"Private environment file in source archive: {member.name}")
        if member.isfile() and path.suffix == ".sh":
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Cannot read archived shell script: {member.name}")
            data = stream.read()
            if b"\r\n" in data or not data.startswith(b"#!/usr/bin/env sh\n"):
                raise ValueError(f"Nonportable shell-script bytes: {member.name}")
            shell_scripts.add(path.name)
if not {"demo.sh", "preflight.sh", "package-functions.sh"} <= shell_scripts:
    raise ValueError("Source archive is missing a required shell script")

with zipfile.ZipFile(Path("dist") / f"{stem}-py3-none-any.whl") as wheel:
    if not any(name.endswith("entry_points.txt") for name in wheel.namelist()):
        raise ValueError("Wheel does not define the CLI entry point")
    if f'__version__ = "{__version__}"' not in wheel.read("llm_batch_starter/__init__.py").decode():
        raise ValueError("Wheel and source package versions differ")
print(f"Verified {stem}: portable shell scripts, artifact boundaries, and wheel entry point.")
