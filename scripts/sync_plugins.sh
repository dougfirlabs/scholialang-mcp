#!/usr/bin/env bash
# Sync the canonical plugin MCP server + vendored validator to every plugin
# variant. The three plugin servers MUST stay byte-identical — enforced by
# test_all_plugin_servers_share_same_validator_engine. Claude Code is the
# canonical copy (it has first-class lifecycle hooks); edit it, then run this
# to propagate to the Codex and Ollama variants.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
canonical="$repo_root/plugins/claude-code/scholialang/scripts"
targets=(
  "$repo_root/plugins/codex/scholialang/scripts"
  "$repo_root/plugins/ollama/scholialang/scripts"
)

for dst in "${targets[@]}"; do
  cp "$canonical/scholialang_mcp_server.py" "$dst/scholialang_mcp_server.py"
  cp "$canonical/_scholia_vendored/"*.py "$dst/_scholia_vendored/"
  if [[ -f "$canonical/_scholia_vendored/UPSTREAM.json" ]]; then
    cp "$canonical/_scholia_vendored/UPSTREAM.json" "$dst/_scholia_vendored/UPSTREAM.json"
  else
    rm -f "$dst/_scholia_vendored/UPSTREAM.json"
  fi
  echo "synced -> ${dst#"$repo_root/"}"
done
echo "done"
