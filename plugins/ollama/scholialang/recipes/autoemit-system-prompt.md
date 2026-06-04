# Scholialang Auto-Emit System Prompt (Ollama / generic MCP hosts)

Ollama-backed and other generic MCP hosts have no lifecycle hooks, so auto-emit
is model-driven and **best-effort**. Paste the block below into your host's
**system prompt** (Continue, Cline, open-webui, or any stdio MCP host) so the
model keeps a per-project Scholialang trace by default.

---

You have a Scholialang MCP server for local, per-project work traces. By default,
keep a trace for the current project:

1. At the start of any non-trivial task, call `scholia_dag_ensure_session` once
   with `project_path` (the workspace root), `host` (your host name, e.g.
   `"ollama"`, `"continue"`, `"cline"`), and a stable `session_id` for this
   conversation. It is idempotent — repeat calls return the same session DAG.
2. If it returns `{"enabled": false, "skipped": true}`, auto-emit is opted out —
   do not create or force a trace.
3. Otherwise, as you work, append concise atoms with `scholia_dag_add_atom`
   (using the returned `dag_id`): `Observation`, `Deciding` (name the chosen
   path), `Finding`, `Action` (durable changes), and `Contradiction`/`Retract`
   when a prior atom is invalidated. Link atoms via `links` (`derived_from`,
   `supports`, `refutes`, `depends_on`).
4. At the end, append a `Concluding` atom referencing the trace `Goal`.

Keep summaries short, reference files by path rather than pasting contents, and
never record secrets or hidden chain-of-thought.

---

## Opt-out

Auto-emit is on by default. To disable:

- **Globally:** set `SCHOLIA_AUTOEMIT=0` (or `false`/`off`) in the environment
  the harness uses to spawn the MCP server.
- **Per repo:** create a `.scholia-off` file in the project root.

Both are enforced inside the MCP server, so the same switch is honored no matter
which host (Ollama, Codex, Claude Code) is driving the trace.
