"""Fail closed if pytest aliases a host bundle's tests to another module."""
from pathlib import Path

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        module = getattr(item, "module", None)
        module_file = getattr(module, "__file__", None)
        if module_file and Path(module_file).resolve() != Path(item.path).resolve():
            raise pytest.UsageError(
                f"test collection identity mismatch: {item.nodeid} loaded {module_file}"
            )
