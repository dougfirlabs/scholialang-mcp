---
name: scholialang-verify
description: Use when someone asks whether Scholialang actually works end to end, whether the advertised MCP tools, DAG lifecycle, validator rules, or an installed wheel behave as documented, or when a release or plugin copy needs functional verification beyond version parity. Runs a deterministic scenario battery (positive, negative, composition, installed-artifact) against isolated sandboxes and reports pass, incomplete, or fail per scenario. It never touches the user's real DAG store or configuration.
metadata:
  version: "0.7.2"
  grammar: "0.6.2"
---

# Scholialang Verify

Prove functionality, not just version parity, by running the bundled runner —
never by declaring surfaces healthy from memory. Every scenario executes
against throwaway sandbox homes, stores, projects, and install targets; the
user's real `~/.scholialang` database, plugin cache, and project traces are
never read or written.

## Run

```bash
# Default arms: canonical plugin, one vendored plugin copy, installed wheel.
python3 scripts/scholia_verify.py --root <checkout> --evidence-dir <dir> --json

# One arm at a time, or with a prebuilt wheel fixture:
python3 scripts/scholia_verify.py --root <checkout> --evidence-dir <dir> \
  --arm canonical-plugin --arm installed-wheel --wheel dist/scholialang_mcp-0.7.2-py3-none-any.whl

# Print the scenario manifest without running anything:
python3 scripts/scholia_verify.py --list --evidence-dir <dir>
# Exit code: 0 pass, 1 incomplete, 2 fail.
```

Useful switches: `--fixtures <dir>` (shared scholialang-spec conformance
corpus; defaults to the checkout's vendored copy), `--skip <scenario-id>`
(marks the scenario `not_run` — a skipped required scenario is never a pass),
`--keep-sandboxes` (retain the throwaway directories for inspection).

## Read the report

- `verify_report.json` plus one evidence file per scenario land in
  `--evidence-dir`. Evidence records the manifest entry, named checks, and
  normalized raw request/response exchanges (paths, timestamps, and minted
  ids are replaced with placeholders, so repeated runs are byte-identical).
- Scenario statuses are `pass`, `fail`, `unsupported`, or `not_run`.
  `overall.verdict` is derived from required scenarios only: `pass` needs
  every required scenario to pass, any required failure is `fail`, and a
  required skip/unsupported makes the run `incomplete`.
- Arms: `canonical-plugin`, `vendored-codex`, and `vendored-ollama` drive the
  plugin MCP servers over stdio JSON-RPC; `installed-wheel` builds (or takes
  via `--wheel`) a release wheel, installs it into a clean target site, and
  drives `python -m scholialang_mcp` end to end.

## Answering the user

Quote the per-scenario statuses and the overall verdict, and name any
scenario that failed, was unsupported, or did not run — never summarize an
`incomplete` run as passing. Version questions belong to scholialang-doctor;
this skill answers "does it actually work?".
