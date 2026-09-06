---
name: "scholialang"
description: "Use when the user wants Claude Code to capture, inspect, summarize, search, compact, export, lint, or reason from explicit local Scholialang DAG traces. Also use when the user asks about Scholialang atoms, relations, operators, examples, validator rules, or local graph history."
---

# Scholialang

Use the `scholialang` MCP server to record, retrieve, import, export, and
**validate** explicit work traces as local SQLite-backed DAGs. Atoms are nodes;
references, evidence, implications, contradictions, and retractions are edges.
These traces are user-facing work artifacts, not hidden chain-of-thought.

The packaged server exposes Scholialang MCP tools with underscore names, such as
`scholia_dag_start`. Dotted names such as `scholia.dag_start` are legacy aliases
for hosts that allow dots in tool names.

## Auto-Emit (Default)

This plugin auto-emits by default, per project. On Claude Code the SessionStart
hook calls `scholia_dag_ensure_session` (host `claude-code`) to open or resume a
per-project session DAG and injects its `dag_id` into context; the SessionEnd
hook appends a closing `Summary`. Your job in between is to append semantic atoms
at meaningful boundaries with `scholia_dag_add_atom` (using that `dag_id`):

- `Observation` for command/file/query results that matter.
- `Deciding` at branch points, naming the chosen path.
- `Finding` for conclusions; `Action` for durable external changes.
- `Contradiction` / `Retract` when something invalidates a prior atom.
- A `Concluding` atom at the end, linked back to the trace `Goal`.

Link atoms with `links` (`derived_from`, `supports`, `refutes`, `depends_on`).
Each session is its own DAG, tagged `host:<host>` and `session:<id>`, so the same
project used from both Claude Code and Codex yields distinct, tool-labeled traces
in one project bucket. `scholia_dag_ensure_session` is idempotent — calling it
again returns the same session DAG rather than creating a duplicate.

**Opt-out (one switch, honored by every host):** set `SCHOLIA_AUTOEMIT=0`
(or `false`/`off`) to disable globally, or create a `.scholia-off` file in the
project root to disable for that repo. Explicit, user-requested tracing still
works when auto-emit is off.

## When To Start Or Append

- If the user asks to use Scholialang, track a trace, dogfood traces, summarize decisions, or preserve context, call `scholia_dag_start` for the current project if there is no active DAG. `scholia_dag_start` creates the trace-level `Goal` atom from the objective.
- Append concise explicit artifacts at meaningful boundaries: goal, observation, hypothesis, evidence, finding, concluding, decision, action, contradiction, retraction, or summary.
- Use `scholia_dag_add_atom` with `links` whenever the new atom depends on, supports, refutes, implies, contradicts, or retracts prior atoms.
- Use `Concluding` for final or checkpoint conclusions, especially when closing a `Goal`; link it back to the goal with `derived_from` or `refers` and include the goal status in the summary/content.
- Do not record secrets, credentials, private keys, raw customer data, or hidden chain-of-thought.
- Prefer short summaries plus file references over copying large file contents into a trace.

## When To Lint

- Call `scholia_lint_snippet` when the user pastes a Scholia trace and asks if it's valid, or before storing a fragment that will be referenced elsewhere. The default `mode='full'` uses the package's 0.7.3 validator for the Scholia v0.7.0 language grammar — closed-set atom kinds, canonical_id/fingerprint well-formedness, reference completeness, decision closure, action recording, hypothesis evaluation, retract consistency, constraint respect, goal declaration, operator vocabulary, optional field shapes, and Concluding closure rules with warnings.
- Use `mode='tag_balance'` for fast syntactic-only checks when full grammar isn't needed.
- Call `scholia_lint_trace` when you need a per-rule breakdown for CI gates, dashboards, or "which rule failed" questions — the response includes `errors_by_rule` and `counts_by_rule` keyed on the canonical `RULE_NAMES`.

## Token Discipline

- Use `scholia_dag_summary`, `scholia_dag_frontier`, `scholia_dag_neighbors`, `scholia_dag_search`, and `scholia_dag_list` before reading full graph content.
- Call `scholia_dag_read` with bounded `limit` and avoid `include_nodes=true` unless exact atoms are needed.
- Call `scholia_dag_compact` before carrying old work across threads or long-running sessions.
- Use trace IDs and atom IDs in conversation instead of pasting whole traces.
- The default local database is `~/.scholialang/scholialang.sqlite3`; only mention or inspect it when the user asks about storage/debugging.

## Project Path

When a tool accepts `project_path`, pass the current repository or workspace
root. If unavailable, omit it and the MCP server will use its global store.

## Useful Flow

1. `scholia_dag_start` with the project path, title, objective, and tags; use the returned `goal_atom` as the trace goal.
2. `scholia_dag_add_atom` after important observations, decisions, and actions.
3. `scholia_dag_add_atom` with kind `Concluding` when a goal or checkpoint is closed.
4. `scholia_dag_link` when a relationship becomes clear after both nodes exist.
5. `scholia_lint_snippet` (mode='full') before persisting a trace fragment the user authored.
6. `scholia_dag_frontier` and `scholia_dag_summary` when resuming work.
7. `scholia_dag_neighbors` for focused recall around one atom.
8. `scholia_dag_search` to locate prior decisions or findings.
9. `scholia_dag_export` only when the user asks for a sharable artifact.
