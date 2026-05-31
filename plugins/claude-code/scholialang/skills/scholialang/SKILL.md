---
name: "scholialang"
description: "Use when the user wants Claude Code to capture, inspect, summarize, search, compact, export, lint, or reason from explicit local Scholialang DAG traces. Also use when the user asks about Scholialang atoms, relations, operators, examples, validator rules, or local graph history."
---

# Scholialang

Use the `scholialang` MCP server to record, retrieve, import, export, and
**validate** explicit work traces as local SQLite-backed DAGs. Atoms are nodes;
references, evidence, implications, contradictions, and retractions are edges.
These traces are user-facing work artifacts, not hidden chain-of-thought.

## When To Start Or Append

- If the user asks to use Scholialang, track a trace, dogfood traces, summarize decisions, or preserve context, call `scholia.dag_start` for the current project if there is no active DAG. `dag_start` creates the trace-level `Goal` atom from the objective.
- Append concise explicit artifacts at meaningful boundaries: goal, observation, hypothesis, evidence, finding, concluding, decision, action, contradiction, retraction, or summary.
- Use `scholia.dag_add_atom` with `links` whenever the new atom depends on, supports, refutes, implies, contradicts, or retracts prior atoms.
- Use `Concluding` for final or checkpoint conclusions, especially when closing a `Goal`; link it back to the goal with `derived_from` or `refers` and include the goal status in the summary/content.
- Do not record secrets, credentials, private keys, raw customer data, or hidden chain-of-thought.
- Prefer short summaries plus file references over copying large file contents into a trace.

## When To Lint

- Call `scholia.lint_snippet` when the user pastes a Scholia trace and asks if it's valid, or before storing a fragment that will be referenced elsewhere. The default `mode='full'` runs the complete v0.4 grammar — closed-set atom kinds, reference completeness, decision closure, action recording, hypothesis evaluation, retract consistency, constraint respect, goal declaration, operator vocabulary, and v0.3.1 optional field shapes.
- Use `mode='tag_balance'` for fast syntactic-only checks when full grammar isn't needed.
- Call `scholia.lint_trace` when you need a per-rule breakdown for CI gates, dashboards, or "which rule failed" questions — the response includes `errors_by_rule` and `counts_by_rule` keyed on the canonical `RULE_NAMES`.

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
5. `scholia.lint_snippet` (mode='full') before persisting a trace fragment the user authored.
6. `scholia.dag_frontier` and `scholia.dag_summary` when resuming work.
7. `scholia.dag_neighbors` for focused recall around one atom.
8. `scholia.dag_search` to locate prior decisions or findings.
9. `scholia.dag_export` only when the user asks for a sharable artifact.
