# Scholia Live — Claude Code plugin viewer (design)

- **Date:** 2026-06-08
- **Status:** Approved design (revised to reuse existing dashboard), pending spec review
- **Target repo:** `scholialang-mcp` → `plugins/claude-code/scholialang/`
- **Approach:** REUSE the user's existing standalone dashboard; do not build from scratch.

## Summary

Add an opt-in, config-gated **Scholia Live** web viewer to the Claude Code
plugin by **reusing the existing standalone dashboard** the user already built:

`~/Dropbox/Projects/scholia-website/plugins/scholialang/scripts/scholialang_webview_server.py`

When enabled, the `SessionStart` hook launches that dashboard as a background
local web server and prints its URL. The page shows the current project's
session DAG and live-streams new atoms via SSE.

This supersedes the earlier from-scratch `scholia_live.py` design — that work is
unnecessary because the dashboard already exists, is stdlib-only, and is
API-compatible with the plugin.

## The reuse artifact (verified)

`scholialang_webview_server.py` (≈2770 lines):
- **Runtime:** Python 3 **stdlib only** — `http.server.ThreadingHTTPServer`.
  No Flask/FastAPI/node.
- **Routes (read-only, `do_GET` only):**
  - `GET /` → full embedded HTML/CSS/JS UI (Checkpoint/Exhaust views, AST
    connections, pagination, theming).
  - `GET /health` → `{ok, database_path, project_path}`.
  - `GET /api/dags?project_path=&limit=` → DAG list for the project.
  - `GET /api/snapshot?dag_id=&project_path=&limit=` → atoms/edges/frontier/AST.
  - `GET /events?project_path=&dag_id=&interval=` → **SSE** stream; polls the
    snapshot (default 0.75s), pushes a `snapshot` event on change, keepalive
    otherwise, up to 8h.
- **Data source:** `import scholialang_mcp_server as scholia` from its own
  `scripts/` dir (it inserts `SCRIPT_DIR` into `sys.path`); reads
  `scholia.database_path()` (honors `SCHOLIALANG_HOME`).
- **CLI:** `--host (127.0.0.1) --port (8765) --project-path --poll-interval
  (0.75) --open-chrome --quiet`.

**Compatibility (verified):** the only `scholia.*` functions the dashboard calls
are `database_path, now, load_dag, all_dags, frontier_nodes, dag_metadata,
build_summary` — **all present** in the plugin's own `scripts/scholialang_mcp_server.py`
(which is newer: 2393 vs 2138 lines). Therefore we copy **only**
`scholialang_webview_server.py`; it resolves its import against the plugin's
existing server module. No need to bring the dashboard's older server copy or a
second `_scholia_vendored/`.

**Smoke-tested live:** launched from its current location against
`~/.scholialang/scholialang.sqlite3` with `--project-path /Users/barrysevig128`;
`/health` OK and `/api/dags` returned this session's DAG
(`dag_20260608T074103Z_dffc5408`, 8 nodes). The viewer works.

## What we actually build (small)

1. **Vendor one file:** copy `scholialang_webview_server.py` into
   `plugins/claude-code/scholialang/scripts/` (source repo) and mirror into the
   installed cache `…/0.3.2/scripts/`.
2. **Config switch:** env var **`SCHOLIA_LIVE`** — off by default; on for
   `1/true/on/yes` (mirrors the `SCHOLIA_AUTOEMIT` parser, opt-in). Optional
   **`SCHOLIA_LIVE_PORT`** (default `8765`, matching the dashboard).
3. **Hook launch:** in `scripts/hooks/session_start.py`, when `SCHOLIA_LIVE` is
   enabled, ensure a **singleton** dashboard process is running and print
   `Scholia Live: http://127.0.0.1:<port>/?project_path=<cwd>`.
   - Launch: `python3 <plugin>/scripts/scholialang_webview_server.py --host
     127.0.0.1 --port <port> --project-path <cwd> --quiet` (no `--open-chrome`,
     per "print URL, don't auto-open").
   - Singleton via `${SCHOLIALANG_HOME:-~/.scholialang}/live-server.json`
     `{pid, port, started_at}`: if the pid is alive, reuse and reprint the URL;
     else start fresh. One server per machine (the SQLite DB is shared); the page
     scopes per project via `?project_path=`.
4. **Version + docs:** bump plugin `0.3.2 → 0.3.3`; add a short "Scholia Live"
   section to the plugin README (enable flag, URL, how to stop).

## Data flow

```
SessionStart hook ──(if SCHOLIA_LIVE on)──> ensure singleton webview_server.py
                                            (127.0.0.1:PORT, --quiet)  ─prints URL→ session
browser GET /?project_path=<cwd>
   └─ EventSource /events ──> server polls scholia.load_dag(DB) ──> snapshot events (live)
```

## Lifecycle & safety

- Binds `127.0.0.1` only; local single-user; no auth. Read-only DB access.
- Read-only viewer — no filesystem writes at all in v1.
- Daemon outlives a single session (other sessions reuse it). `SessionEnd` does
  not kill it. Stop = kill the pid in `live-server.json` (the hook owns it; we
  add a tiny `--stop`-by-pidfile path or document `kill $(jq .pid …)`).
- No secrets in payloads (atoms already exclude secrets by plugin policy).

## Open scope decision (for review)

The existing dashboard is **read-only** — it has no auto-emit toggle. Two ways to
handle the "settings panel" the earlier design mentioned:

- **(A, recommended) Ship read-only v1.** Maximum reuse, zero new write surface.
  The auto-emit toggle stays where it already works (env var / `.scholia-off`
  file). Add a settings-write panel later only if wanted.
- **(B) Add a settings-write panel now.** Introduce `do_POST /api/settings` to
  toggle `.scholia-off` and add a Settings tab to the embedded UI. More code,
  adds a write surface to the viewer.

## Testing

- **Unit:** `SCHOLIA_LIVE` parser (on/off matrix); hook launch builds the right
  argv and writes/reuses the pidfile (singleton); stale-pid restart.
- **Import check:** `scholialang_webview_server.py` imports cleanly when located
  in the plugin `scripts/` dir (resolves the plugin's `scholialang_mcp_server`).
- **Manual smoke:** set `SCHOLIA_LIVE=1`, start a session, open the printed URL,
  emit an atom, confirm it appears live via SSE within one poll interval.

## Out of scope / later

- `do_POST` settings-write panel (option B); idle auto-shutdown; project/session
  switcher polish; any change to the dashboard's UI beyond what ships today.
