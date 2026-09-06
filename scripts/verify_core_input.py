#!/usr/bin/env python3
"""Verify the retained local core input before an offline installation."""
from __future__ import annotations

import hashlib
import json
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path


def verify(root: Path) -> dict:
    directory = root / "vendor/core"
    receipt = json.loads((directory / "RECEIPT.json").read_text())
    for name, digest in receipt["artifacts"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"artifact hash mismatch: {name}")
    with zipfile.ZipFile(directory / "scholialang-0.7.3-py3-none-any.whl") as wheel:
        metadata = BytesParser().parsebytes(wheel.read("scholialang-0.7.3.dist-info/METADATA"))
        if metadata.get_all("Requires-Dist") != [
            "pyyaml>=6.0", 'pytest>=7.0; extra == "dev"', 'pytest-timeout>=2.0; extra == "dev"'
        ]:
            raise ValueError("unexpected core dependency closure")
        with tarfile.open(directory / "scholialang-0.7.3.tar.gz") as sdist:
            for name in wheel.namelist():
                if name.startswith("scholialang/") and name.endswith(".py"):
                    member = sdist.extractfile(f"scholialang-0.7.3/src/{name}")
                    if member is None or member.read() != wheel.read(name):
                        raise ValueError(f"wheel/sdist source mismatch: {name}")
            for name in ("LICENSE-MIT", "LICENSE-APACHE"):
                member = sdist.extractfile(f"scholialang-0.7.3/{name}")
                expected = (directory / name).read_bytes()
                if member is None or member.read() != expected:
                    raise ValueError(f"sdist license mismatch: {name}")
                if wheel.read(f"scholialang-0.7.3.dist-info/licenses/{name}") != expected:
                    raise ValueError(f"wheel license mismatch: {name}")
    with zipfile.ZipFile(directory / "pyyaml-6.0.3-py3-none-any.whl") as wheel:
        metadata = BytesParser().parsebytes(wheel.read("pyyaml-6.0.3.dist-info/METADATA"))
        if metadata.get_all("Requires-Dist") or metadata["License"] != "MIT":
            raise ValueError("unexpected PyYAML dependency/license closure")
        with tarfile.open(directory / "pyyaml-6.0.3.tar.gz") as sdist:
            for name in wheel.namelist():
                if name.endswith(".py"):
                    member = sdist.extractfile(f"pyyaml-6.0.3/lib/{name}")
                    if member is None or member.read() != wheel.read(name):
                        raise ValueError(f"PyYAML wheel/sdist mismatch: {name}")
            member = sdist.extractfile("pyyaml-6.0.3/LICENSE")
            expected = (directory / "LICENSE-PyYAML").read_bytes()
            if member is None or member.read() != expected:
                raise ValueError("PyYAML sdist license mismatch")
            if wheel.read("pyyaml-6.0.3.dist-info/licenses/LICENSE") != expected:
                raise ValueError("PyYAML wheel license mismatch")
    return receipt


if __name__ == "__main__":
    print(json.dumps(verify(Path(__file__).resolve().parents[1]), indent=2, sort_keys=True))
