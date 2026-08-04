# Scholialang for Claude Desktop

This directory is the source template for the forwardable Scholialang MCP
Bundle (`.mcpb`). The release artifact is assembled from this template and the
canonical server under `plugins/claude-code/scholialang/scripts/`; the server
is not copied into git a fourth time.

## Install

1. Open Claude Desktop and choose **Settings → Extensions**.
2. Open **Advanced settings → Extension Developer**.
3. Choose **Install Extension…** and select the supplied `.mcpb` file.
4. Restart Claude Desktop if its Connectors list does not refresh immediately.
5. In a chat, open **+ → Connectors** and confirm that **Scholialang** is
   enabled.

Smoke prompt:

> Use Scholialang to start a local DAG for this project. Add a hypothesis,
> observation, evidence, and finding, link the finding to its evidence, and
> summarize the frontier.

## Privacy and storage

The server is local-only and makes no network calls. Its default working store
is `~/.scholialang/scholialang.sqlite3`. Do not put credentials, raw customer
PII, or hidden chain-of-thought into traces. Use short claims and references to
reviewed source material.

## Build

From the repository root:

```sh
python3 scripts/build_claude_desktop_mcpb.py --output dist/scholialang-claude-desktop-0.7.2.mcpb
```

The build stages the canonical stdio server and vendored validator, validates
the manifest with the pinned official MCPB CLI, packs the archive, and prints
its SHA-256 digest.
