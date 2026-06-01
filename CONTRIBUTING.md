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
