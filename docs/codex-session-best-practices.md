# Codex Session Best Practices

Scholialang works best as a quiet project memory layer during normal Codex
sessions and as an explicit audit trail when the user asks for full exhaust.

## Defaults

- Do not import full Codex exhaust automatically.
- Do not post trace links into every chat response.
- Do not export trace review artifacts unless the user asks for them.
- Keep visible trace entries concise: findings, decisions, observations, and
  action summaries.
- Keep hidden or encrypted reasoning as hash/reference metadata only.

## Recommended Session Flow

At the start of substantial project work:

1. Use `scholia.dag_list` or `scholia.dag_search` with the current
   `project_path` to locate recent project traces.
2. Use `scholia.dag_summary`, `scholia.dag_frontier`, or
   `scholia.dag_neighbors` for bounded recall.
3. Start a new DAG only when there is no active trace for the work.

During work:

1. Append only meaningful state changes: observations, evidence, findings,
   decisions, contradictions, retractions, actions, and summaries.
2. Link atoms with `derived_from`, `supports`, `refutes`, `depends_on`, or
   another explicit relation whenever the dependency matters.
3. Prefer file references and short summaries over copying long command output.

Before ending substantial work:

1. Add a concise summary atom with the current state.
2. Compact the DAG with `scholia.dag_compact` if the session produced many
   atoms.
3. Export only when the user asks for a sharable artifact or review UI.

## Full Exhaust Imports

Use `scholia.codex_import_thread` only for audit, debugging, provenance, or
token/cost analysis. Full exhaust imports intentionally preserve repeated raw
Codex event surfaces. They are not optimized as first-read documents.

For human review, prefer:

- `scholia.dag_summary`
- `scholia.dag_frontier`
- `scholia.dag_search`
- `scholia.dag_export` with `format: "html"` and `write_file: true`

## Lightweight Trace Review UI

`scholia.dag_export` supports a standalone HTML trace viewer:

```json
{
  "dag_id": "dag_...",
  "project_path": "/path/to/project",
  "format": "html",
  "write_file": true,
  "include_trace_link": false
}
```

The HTML export is intentionally default-off. It writes to the active
Scholialang storage root under `exports/`, which means project-local setups
write under:

```text
/path/to/project/.scholialang/exports/
```

Keep generated review HTML ignored by default. Commit curated SRML or Markdown
summaries only after review.

## Project-Local Storage

For project work:

```sh
cd /path/to/project
export SCHOLIALANG_HOME="$PWD/.scholialang"
codex
```

Recommended `.gitignore`:

```gitignore
.scholialang/*.sqlite3
.scholialang/*.sqlite3-*
.scholialang/exports/
```
