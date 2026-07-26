# Contributing

Use focused pull requests and include tests for protocol behavior changes.

Before submitting:

```sh
pytest
python3 -m unittest plugins.codex.scholialang.tests.test_scholialang_mcp_server
```

Protocol changes should preserve backwards-compatible MCP and LSP responses
unless the README and tests are updated in the same change.

Codex plugin changes should also validate the plugin manifest and local MCP
server:

```sh
python3 -m py_compile plugins/codex/scholialang/scripts/scholialang_mcp_server.py
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/codex/scholialang
```

## Release preparation

Refresh the canonical plugin validator from an exact, reviewed commit in the
public `scholialang` repository, then propagate it byte-identically:

```sh
python scripts/vendor_scholialang.py ../scholialang <merged-commit-sha>
scripts/sync_plugins.sh
PYTHONPATH=src pytest
python -m build
python -m twine check dist/*
```

`vendor_scholialang.py` reads Git blobs from the named commit rather than the
source checkout's working tree, rewrites only the package-relative imports
needed by the standalone plugins, and records source and rendered hashes in
`UPSTREAM.json`. Never hand-edit one plugin's vendored validator copy.

Publish `scholialang` first. The MCP distribution declares its matching
minimum dependency and is intentionally limited to the MCP/LSP Python core;
host plugins and Scholia Live are released as marketplace artifacts.
