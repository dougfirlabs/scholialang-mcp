# MCP protocol support matrix

**Generated from live wire probes** by
`tests/integration/test_mcp_support_matrix.py` — do not edit by hand.
Regenerate with:

```sh
SCHOLIA_REGEN_SUPPORT_MATRIX=1 python -m pytest tests/integration/test_mcp_support_matrix.py
```

The 2026-07-28 cell passes a stateless `_meta`-carried request and
confirms removed lifecycle methods return `-32601 MethodNotFound`.
Pre-2026 cells pass the legacy `initialize` lifecycle at that version.
Rejected cells fail closed with `-32022 UnsupportedProtocolVersion`
and include the final-stable `supported` / `requested` error data.

| protocol version | wheel (`scholialang-mcp serve`) | plugin (vendored, all hosts) |
|---|---|---|
| `2026-07-28` | modern stateless | modern stateless |
| `2025-11-25` | legacy handshake | legacy handshake |
| `2025-06-18` | legacy handshake | legacy handshake |
| `2025-03-26` | legacy handshake | legacy handshake |
| `2024-11-05` | legacy handshake | rejected (-32022) |
| `1999-01-01` (control) | rejected (-32022) | rejected (-32022) |
| `server/discover` | yes: 2026-07-28, 2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05 | yes: 2026-07-28, 2025-11-25, 2025-06-18, 2025-03-26 |
