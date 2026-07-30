# MCP protocol support matrix

**Generated from live wire probes** by
`tests/integration/test_mcp_support_matrix.py` — do not edit by hand.
Regenerate with:

```sh
SCHOLIA_REGEN_SUPPORT_MATRIX=1 python -m pytest tests/integration/test_mcp_support_matrix.py
```

Each *supported* cell means the server passed both a legacy
`initialize` handshake at that version and a stateless 2026-07-28
`_meta`-carried request at that version. Each *rejected* cell means
both probes failed closed with `-32022 UnsupportedProtocolVersion`
(never a silent fallback).

| protocol version | wheel (`scholialang-mcp serve`) | plugin (vendored, all hosts) |
|---|---|---|
| `2026-07-28` | supported | supported |
| `2025-11-25` | supported | supported |
| `2025-06-18` | supported | supported |
| `2025-03-26` | supported | supported |
| `2024-11-05` | supported | rejected (-32022) |
| `1999-01-01` (control) | rejected (-32022) | rejected (-32022) |
| `server/discover` | yes: 2026-07-28, 2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05 | yes: 2026-07-28, 2025-11-25, 2025-06-18, 2025-03-26 |
