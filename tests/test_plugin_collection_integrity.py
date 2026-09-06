"""Configured bundle tests must be collected from their actual source files."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUNDLES = ("claude-code", "codex", "ollama")


def test_bundle_collection_preserves_module_identity_and_test_methods(request):
    assert request.config.getini("consider_namespace_packages") is True
    configured = request.config.getini("testpaths")
    modules = {}
    for host in BUNDLES:
        directory = f"plugins/{host}/scholialang/tests"
        assert directory in configured
        path = (ROOT / directory / "test_scholialang_mcp_server.py").resolve()
        items = [item for item in request.session.items if Path(item.path).resolve() == path]
        # Focused invocations may intentionally select another part of the suite.
        # The default full suite includes each configured directory above.
        if not items:
            continue
        for item in items:
            assert Path(item.module.__file__).resolve() == path
            assert item.module.__name__ not in modules or modules[item.module.__name__] == path
            modules[item.module.__name__] = path
        tree = ast.parse(path.read_text(encoding="utf-8"))
        expected = {
            (cls.name, node.name)
            for cls in tree.body if isinstance(cls, ast.ClassDef)
            for node in cls.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        }
        # A -k/-m/node-id selection may deliberately choose only some methods.
        # Identity is still checked for every selected item; completeness applies
        # to an unfiltered default-suite invocation.
        if (not request.config.option.keyword and not request.config.option.markexpr
                and set(configured).issubset(request.config.args)):
            actual = {(item.cls.__name__, item.originalname or item.name) for item in items}
            assert actual == expected


def test_collection_guard_rejects_a_module_loaded_from_the_wrong_bundle(tmp_path):
    spec = importlib.util.spec_from_file_location("collection_guard_under_test", ROOT / "conftest.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    requested = tmp_path / "requested.py"
    item = SimpleNamespace(path=requested, nodeid="requested.py::test_case",
                           module=SimpleNamespace(__file__=str(requested)))
    guard.pytest_collection_modifyitems([item])
    item.module.__file__ = str(tmp_path / "other_bundle.py")
    with pytest.raises(pytest.UsageError, match="collection identity mismatch"):
        guard.pytest_collection_modifyitems([item])
