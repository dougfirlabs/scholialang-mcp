---
name: scholialang-doctor
description: Use when someone asks whether Scholialang is healthy, which Scholialang package, plugin, vendored validator, grammar, or skill versions are loaded, why MCP or LSP tools are missing, whether auto-emit is on, or whether these surfaces are compatible. Runs a deterministic read-only doctor script that reports per-facet versions and an overall pass, not_ready, or fail. It never installs, upgrades, authenticates, or edits configuration.
metadata:
  version: "0.7.3"
  grammar: "0.6.2"
---

# Scholialang Doctor

Answer "what is loaded, which grammar is implemented, and are these surfaces
compatible?" by running the bundled script — never by guessing from memory.
The doctor is strictly read-only: it must not install, upgrade, authenticate,
edit configuration, initialize a DAG, or repair a database. Fixes appear only
as `recommendations` text for the user to apply manually.

## Run

```bash
# Inspect a scholialang-mcp checkout:
python3 scripts/scholia_doctor.py --mode repo --root <checkout> --json

# Inspect installed distributions (no checkout needed):
python3 scripts/scholia_doctor.py --mode installed --json

# Default --mode auto picks repo when --root (or the CWD) is a checkout.
# Omit --json for a short human summary. Exit code: 0 pass, 1 not_ready, 2 fail.
```

Useful switches: `--project <dir>` (where to look for a `.scholia-off`
opt-out), `--data-dir <dir>` (Scholialang data directory, default
`~/.scholialang`), `--site <dir>` (explicit metadata search path for
installed mode; repeatable).

## Read the report

- `facets` — one entry per axis, each with `supported` / `present` /
  `version` / `compatible` / `detail`: `grammar`, `python_package`
  (scholialang), `mcp_package` (scholialang-mcp), `plugin`,
  `vendored_validator`, `skill`, `mcp_entry_point`, `lsp_entry_point`,
  `auto_emit`, `database`, `compatibility`.
- Grammar and release are distinct axes: release 0.7.3 implements the stable
  Scholia grammar v0.6.2. Never describe that relationship as a downgrade.
- `overall.status` is `pass`, `not_ready`, or `fail`, with per-facet
  `reasons`. Missing optional surfaces (for example, no local database yet)
  are reported per-facet and are not generic failures.
- The `database` facet is reachability metadata only (path, existence, size);
  the doctor never opens the database or reads DAG contents, and the report
  never contains environment values.

## Answering the user

Quote the relevant facet versions and the overall status, then relay any
`recommendations` as suggested manual steps. Do not run installers or edit
configuration on the user's behalf as part of a doctor check.
