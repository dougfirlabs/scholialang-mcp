---
name: "scholialang"
description: "Use when the user wants Codex to capture, inspect, summarize, search, compact, export, or reason from explicit local Scholialang DAG traces. Also use when the user asks about Scholialang atoms, relations, operators, examples, or local graph history."
---

# Scholialang

Use the `scholialang` MCP server to record, retrieve, import, and export
explicit work traces as local SQLite-backed DAGs. Atoms are nodes; references,
evidence, implications, contradictions, and retractions are edges. These traces
are user-facing work artifacts, not hidden chain-of-thought.

## When To Start Or Append

- If the user asks to use Scholialang, track a trace, dogfood traces, summarize decisions, or preserve context, call `scholia.dag_start` for the current project if there is no active DAG. `dag_start` creates the trace-level `Goal` atom from the objective.
- If the user asks for Codex exhaust, rollout history, full prompt/tool trails, or token usage from a Codex thread, use `scholia.codex_import_thread` against the relevant Codex rollout JSONL.
- Append concise explicit artifacts at meaningful boundaries: goal, observation, hypothesis, evidence, finding, concluding, decision, action, contradiction, retraction, or summary.
- Use `scholia.dag_add_atom` with `links` whenever the new atom depends on, supports, refutes, implies, contradicts, or retracts prior atoms.
- Use `Concluding` for final or checkpoint conclusions, especially when closing a `Goal`; link it back to the goal with `derived_from` or `refers` and include the goal status in the summary/content.
- Do not record secrets, credentials, private keys, raw customer data, or hidden chain-of-thought.
- Prefer short summaries plus file references over copying large file contents into a trace.

## Codex Exhaust Imports

`scholia.codex_import_thread` is for durable audit trails. It preserves raw
Codex rollout events and derives canonical OpenTalon-style events for
tool/message/token reasoning. A full exhaust import is intentionally verbose and
may contain repeated user or assistant surfaces because Codex logs the same
semantic message through multiple event channels. Keep that raw DAG intact for
provenance; use summaries, search, or a semantic export when the user wants a
deduped reading view.

Hidden or encrypted reasoning must remain hash/reference metadata only. Do not
decode, reconstruct, or narrate private chain-of-thought from an exhaust trace.

## Token Discipline

- Use `scholia.dag_summary`, `scholia.dag_frontier`, `scholia.dag_neighbors`, `scholia.dag_search`, and `scholia.dag_list` before reading full graph content.
- Call `scholia.dag_read` with bounded `limit` and avoid `include_nodes=true` unless exact atoms are needed.
- Call `scholia.dag_compact` before carrying old work across threads or long-running sessions.
- Use trace IDs and atom IDs in conversation instead of pasting whole traces.
- The default local database is `~/.scholialang/scholialang.sqlite3`; only mention or inspect it when the user asks about storage/debugging.

## Project Path

When a tool accepts `project_path`, pass the current repository or workspace
root. If unavailable, omit it and the MCP server will use its global store.

## Useful Flow

1. `scholia.dag_start` with the project path, title, objective, and tags; use the returned `goal_atom` as the trace goal.
2. `scholia.dag_add_atom` after important observations, decisions, and actions.
3. `scholia.dag_add_atom` with kind `Concluding` when a goal or checkpoint is closed.
4. `scholia.dag_link` when a relationship becomes clear after both nodes exist.
5. `scholia.dag_frontier` and `scholia.dag_summary` when resuming work.
6. `scholia.dag_neighbors` for focused recall around one atom.
7. `scholia.dag_search` to locate prior decisions or findings.
8. `scholia.dag_export` only when the user asks for a sharable artifact.
