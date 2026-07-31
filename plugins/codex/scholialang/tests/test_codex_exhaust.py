import importlib.util
import os
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
# The Scholia Live viewer is host-agnostic and reads the shared SQLite DB, so the
# Codex exhaust DAG is paired by the same webview that pairs Claude Code traces.
# The codex plugin ships no webview of its own; load the shared one to assert the
# pairing semantics (no viewer change is required for Codex).
CC_SCRIPTS = Path(__file__).resolve().parents[3] / "claude-code" / "scholialang" / "scripts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "codex_rollout_sample.jsonl"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Load the codex server first under its canonical name so the shared webview's
# ``import scholialang_mcp_server`` reuses it (both read the same shared DB).
server = _load("scholialang_mcp_server", SCRIPTS / "scholialang_mcp_server.py")
cx = _load("codex_exhaust", SCRIPTS / "codex_exhaust.py")
watcher = _load("codex_exhaust_watcher", SCRIPTS / "codex_exhaust_watcher.py")
webview = _load("scholialang_webview_server", CC_SCRIPTS / "scholialang_webview_server.py")


def _lines():
    return FIXTURE.read_text().splitlines()


class ParserTests(unittest.TestCase):
    """The rollout parser reuses the server's per-line event->atom conversion."""

    def test_maps_event_types_to_atom_kinds(self):
        result = cx.parse_rollout_lines(server, _lines(), source="cx.jsonl")
        kinds = [a["kind"] for a in result.atoms]
        self.assertEqual(
            kinds,
            [
                "Observation",   # session_meta
                "Observation",   # task_started
                "Question",      # user message
                "Finding",       # assistant message
                "Action",        # function_call
                "Observation",   # function_call_output
                "Observation",   # reasoning (encrypted, not materialized)
                "Finding",       # agent_message
                "Finding",       # agent_message (with secret)
                "Concluding",    # task_complete
                "Contradiction", # malformed JSON line
            ],
        )

    def test_one_atom_per_nonblank_line(self):
        result = cx.parse_rollout_lines(server, _lines(), source="cx.jsonl")
        self.assertEqual(len(result.atoms), 11)

    def test_reuses_existing_codex_parser(self):
        # The kind/summary come straight from the existing server functions, not a
        # reimplemented parser. Assert they agree for a representative line.
        import json

        obj = json.loads(_lines()[4])  # function_call
        payload = obj["payload"]
        atom = cx.parse_rollout_lines(server, _lines(), source="s").atoms[4]
        self.assertEqual(atom["kind"], server.codex_event_atom_kind(payload["type"], payload))
        self.assertEqual(atom["summary"], server.codex_event_summary(5, obj["type"], payload["type"], payload))

    def test_stable_per_line_ids(self):
        result = cx.parse_rollout_lines(server, _lines(), source="cx.jsonl")
        self.assertEqual(result.atoms[0]["atom_id"], cx.atom_id_for(1))
        self.assertEqual(result.atoms[0]["line"], 1)
        for atom in result.atoms:
            self.assertEqual(atom["atom_id"], cx.atom_id_for(atom["line"]))

    def test_reparse_is_idempotent_by_line_number(self):
        first = cx.parse_rollout_lines(server, _lines(), source="cx.jsonl")
        second = cx.parse_rollout_lines(server, _lines(), source="cx.jsonl")
        self.assertEqual([a["atom_id"] for a in first.atoms], [a["atom_id"] for a in second.atoms])
        ids = [a["atom_id"] for a in first.atoms]
        self.assertEqual(len(ids), len(set(ids)))

    def test_max_events_cap_and_truncation_flag(self):
        result = cx.parse_rollout_lines(server, _lines(), max_events=3, source="cx.jsonl")
        self.assertEqual(len(result.atoms), 3)
        self.assertTrue(result.truncated)
        self.assertEqual(result.scanned, 3)

    def test_secret_and_encrypted_reasoning_are_scrubbed(self):
        result = cx.parse_rollout_lines(server, _lines(), source="cx.jsonl")
        joined = "\n".join(a["content"] for a in result.atoms)
        self.assertNotIn("sk-do-not-capture-this-secret-0002", joined)
        self.assertNotIn("encrypted-private-thoughts-do-not-capture", joined)

    def test_resume_from_start_line(self):
        result = cx.parse_rollout_lines(server, _lines(), start_line=8, source="cx.jsonl")
        self.assertEqual([a["line"] for a in result.atoms], [8, 9, 10, 11])

    def test_session_meta_and_uuid_helpers(self):
        meta = cx.rollout_session_meta(FIXTURE)
        self.assertEqual(meta["session_id"], "thread_codex_01")
        self.assertEqual(meta["cwd"], "/repo/codex-project")
        self.assertEqual(
            cx.session_id_from_rollout_path(
                "rollout-2026-06-15T11-44-51-019ecc99-cc36-7eb0-81ed-17f8e99eb6ee.jsonl"
            ),
            "019ecc99-cc36-7eb0-81ed-17f8e99eb6ee",
        )


class _HomeCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self._saved = {
            k: os.environ.get(k)
            for k in ("SCHOLIALANG_HOME", "SCHOLIA_AUTOEMIT", "SCHOLIA_EXHAUST", "CODEX_HOME")
        }
        os.environ["SCHOLIALANG_HOME"] = self.tempdir.name
        os.environ.pop("SCHOLIA_AUTOEMIT", None)
        os.environ.pop("SCHOLIA_EXHAUST", None)
        self.project = str(Path(self.tempdir.name) / "proj")
        Path(self.project).mkdir(parents=True, exist_ok=True)
        # Discovery filters rollouts by backing-file mtime against a wall-clock
        # window, so stamp the shared fixture fresh each run. Otherwise the
        # watcher tests pass only on a just-checked-out tree (CI) and fail on
        # any working copy older than the window — a checkout-age flake, not a
        # real condition.
        os.utime(FIXTURE, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tempdir.cleanup()

    def _exhaust_dag(self, session_id="thread_codex_01"):
        info = cx.ensure_exhaust_dag(server, project_path=self.project, session_id=session_id)
        self.assertIsNotNone(info)
        return info["dag_id"]


class CaptureTests(_HomeCase):
    """End-to-end capture into a real (temp) exhaust DAG."""

    def test_capture_appends_atoms(self):
        dag_id = self._exhaust_dag()
        res = cx.capture_once(server, rollout_path=str(FIXTURE), dag_id=dag_id, project_path=self.project, max_events=2000)
        self.assertEqual(res.appended, 11)

    def test_capture_is_idempotent(self):
        dag_id = self._exhaust_dag()
        cx.capture_once(server, rollout_path=str(FIXTURE), dag_id=dag_id, project_path=self.project, max_events=2000)
        before = len(server.load_dag(dag_id, self.project)["nodes"])
        res = cx.capture_once(server, rollout_path=str(FIXTURE), dag_id=dag_id, project_path=self.project, max_events=2000)
        after = len(server.load_dag(dag_id, self.project)["nodes"])
        self.assertEqual(res.appended, 0)
        self.assertEqual(before, after)

    def test_capture_resume_does_not_duplicate(self):
        dag_id = self._exhaust_dag()
        first = cx.capture_once(server, rollout_path=str(FIXTURE), dag_id=dag_id, project_path=self.project, max_events=5)
        self.assertEqual(first.appended, 5)
        self.assertTrue(first.truncated)
        second = cx.capture_once(
            server, rollout_path=str(FIXTURE), dag_id=dag_id, project_path=self.project,
            max_events=2000, start_line=first.last_line + 1,
        )
        self.assertEqual(second.appended, 6)
        nodes = server.load_dag(dag_id, self.project)["nodes"]
        cxline_ids = [nid for nid in nodes if nid.startswith("cxline_")]
        self.assertEqual(len(cxline_ids), 11)

    def test_truncation_is_logged(self):
        dag_id = self._exhaust_dag()
        logged = []
        cx.capture_once(
            server, rollout_path=str(FIXTURE), dag_id=dag_id, project_path=self.project,
            max_events=3, log=logged.append,
        )
        self.assertTrue(any("max_events" in str(m) or "truncat" in str(m).lower() for m in logged))

    def test_capture_makes_no_network_calls(self):
        dag_id = self._exhaust_dag()
        original = socket.socket

        def _boom(*args, **kwargs):
            raise AssertionError("capture path attempted a network/socket call")

        socket.socket = _boom
        try:
            res = cx.capture_once(server, rollout_path=str(FIXTURE), dag_id=dag_id, project_path=self.project, max_events=2000)
        finally:
            socket.socket = original
        self.assertEqual(res.appended, 11)


class PairingTests(_HomeCase):
    """The exhaust DAG title-matches and view-mode-pairs the codex checkpoint DAG."""

    def _meta(self, dag_id):
        return webview.enrich_dag_metadata(server.load_dag(dag_id, self.project))

    def test_exhaust_view_mode_and_match_score(self):
        checkpoint = server.tool_dag_ensure_session(
            {"project_path": self.project, "session_id": "thread_codex_01", "host": "codex", "auto": True}
        )["structuredContent"]
        info = cx.ensure_exhaust_dag(server, project_path=self.project, session_id="thread_codex_01")

        cp_meta = self._meta(checkpoint["dag_id"])
        ex_meta = self._meta(info["dag_id"])
        self.assertEqual(webview.trace_view_mode(cp_meta), "checkpoint")
        self.assertEqual(webview.trace_view_mode(ex_meta), "exhaust")
        self.assertGreaterEqual(webview.trace_match_score(cp_meta, ex_meta), 42)

    def test_related_trace_views_pairs_both(self):
        checkpoint = server.tool_dag_ensure_session(
            {"project_path": self.project, "session_id": "thread_codex_01", "host": "codex", "auto": True}
        )["structuredContent"]
        info = cx.ensure_exhaust_dag(server, project_path=self.project, session_id="thread_codex_01")
        dags = [self._meta(d["dag_id"]) for d in server.all_dags(self.project)]
        cp_meta = self._meta(checkpoint["dag_id"])
        views = webview.related_trace_views(cp_meta, dags)
        self.assertIsNotNone(views["checkpoint"])
        self.assertIsNotNone(views["exhaust"])
        self.assertEqual(views["exhaust"]["dag_id"], info["dag_id"])

    def test_opt_out_suppresses_exhaust_dag(self):
        (Path(self.project) / ".scholia-off").write_text("")
        info = cx.ensure_exhaust_dag(server, project_path=self.project, session_id="thread_codex_01")
        self.assertIsNone(info)

    def test_env_opt_out_suppresses_exhaust_dag(self):
        os.environ["SCHOLIA_AUTOEMIT"] = "0"
        try:
            info = cx.ensure_exhaust_dag(server, project_path=self.project, session_id="thread_codex_01")
        finally:
            os.environ.pop("SCHOLIA_AUTOEMIT", None)
        self.assertIsNone(info)


class ConfigTests(unittest.TestCase):
    """Default-on with the documented opt-outs."""

    def test_exhaust_default_on(self):
        self.assertTrue(cx.exhaust_enabled({}))

    def test_explicit_exhaust_off(self):
        for value in ("0", "false", "off", "no"):
            self.assertFalse(cx.exhaust_enabled({"SCHOLIA_EXHAUST": value}))

    def test_shared_autoemit_off(self):
        self.assertFalse(cx.exhaust_enabled({"SCHOLIA_AUTOEMIT": "0"}))

    def test_max_events_pref(self):
        self.assertEqual(cx.max_events_pref({}), cx.DEFAULT_MAX_EVENTS)
        self.assertEqual(cx.max_events_pref({"SCHOLIA_EXHAUST_MAX_EVENTS": "5"}), 5)
        self.assertEqual(cx.max_events_pref({"SCHOLIA_EXHAUST_MAX_EVENTS": "bogus"}), cx.DEFAULT_MAX_EVENTS)


class DiscoveryTests(_HomeCase):
    """Active rollouts are discovered from the Codex thread state DB."""

    def _state_db(self):
        codex_home = Path(self.tempdir.name) / "codex-home"
        codex_home.mkdir(exist_ok=True)
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            conn.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, title TEXT,
                    archived INTEGER, updated_at INTEGER, updated_at_ms INTEGER
                )
                """
            )
            conn.executemany(
                "INSERT INTO threads (id, rollout_path, cwd, title, archived, updated_at, updated_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("thread_codex_01", str(FIXTURE), self.project, "Live exhaust", 0, 200, 200000),
                    ("archived_thread", str(FIXTURE), self.project, "Old", 1, 100, 100000),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        return codex_home

    def test_discovers_active_thread_only(self):
        codex_home = self._state_db()
        rows = cx.discover_active_rollouts(codex_home)
        self.assertEqual([r["session_id"] for r in rows], ["thread_codex_01"])
        self.assertEqual(rows[0]["project_path"], self.project)
        self.assertEqual(rows[0]["rollout_path"], str(FIXTURE))

    def test_window_filters_stale_rollouts(self):
        codex_home = self._state_db()
        # A zero-second window with a clock far in the future drops everything.
        rows = cx.discover_active_rollouts(codex_home, window_seconds=0, now=2 ** 40)
        self.assertEqual(rows, [])


class WatcherTests(_HomeCase):
    """Singleton launch + one-pass capture loop, never breaking a session."""

    def _state_db(self):
        codex_home = Path(self.tempdir.name) / "codex-home"
        codex_home.mkdir(exist_ok=True)
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            conn.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, title TEXT,
                    archived INTEGER, updated_at INTEGER, updated_at_ms INTEGER
                )
                """
            )
            conn.execute(
                "INSERT INTO threads (id, rollout_path, cwd, title, archived, updated_at, updated_at_ms) "
                "VALUES (?, ?, ?, ?, 0, 100, 100000)",
                ("thread_codex_01", str(FIXTURE), self.project, "Live exhaust", ),
            )
            conn.commit()
        finally:
            conn.close()
        return codex_home

    def test_run_once_streams_into_exhaust_dag(self):
        os.environ["CODEX_HOME"] = str(self._state_db())
        watcher.run(home=self.tempdir.name, window_seconds=None, once=True)
        state = watcher.load_state(watcher.rollout_state_path(self.tempdir.name, "thread_codex_01"))
        self.assertIn("dag_id", state)
        nodes = server.load_dag(state["dag_id"], self.project)["nodes"]
        cxline_ids = [nid for nid in nodes if nid.startswith("cxline_")]
        self.assertEqual(len(cxline_ids), 11)

    def test_run_once_is_idempotent(self):
        os.environ["CODEX_HOME"] = str(self._state_db())
        watcher.run(home=self.tempdir.name, window_seconds=None, once=True)
        dag_id = watcher.load_state(watcher.rollout_state_path(self.tempdir.name, "thread_codex_01"))["dag_id"]
        before = len(server.load_dag(dag_id, self.project)["nodes"])
        watcher.run(home=self.tempdir.name, window_seconds=None, once=True)
        after = len(server.load_dag(dag_id, self.project)["nodes"])
        self.assertEqual(before, after)

    def test_maybe_launch_reuses_running_watcher(self):
        # A live pid in the singleton state file -> reuse, no new process spawned.
        home = self.tempdir.name
        watcher.save_state(watcher.watcher_state_path(home), {"pid": os.getpid(), "started_at": "now"})
        result = watcher.maybe_launch(home=home)
        self.assertEqual(result.get("pid"), os.getpid())

    def test_maybe_launch_disabled_is_noop(self):
        result = watcher.maybe_launch(env={"SCHOLIA_EXHAUST": "0"}, home=self.tempdir.name)
        self.assertIsNone(result)
        # No singleton state file is written when disabled.
        self.assertFalse(watcher.watcher_state_path(self.tempdir.name).exists())

    def test_sync_rollout_skips_when_project_missing(self):
        # A rollout with no resolvable cwd is skipped without raising.
        appended = watcher.sync_rollout(
            self.tempdir.name, {"session_id": "x", "project_path": None, "rollout_path": str(FIXTURE)},
            max_events=2000,
        )
        self.assertEqual(appended, 0)

    def test_entrypoint_trigger_never_raises(self):
        # The Codex entrypoint boot trigger is best-effort; disabled -> no spawn,
        # no raise, and the shared server is left untouched (byte-parity).
        entry = _load("codex_mcp_entry", SCRIPTS / "codex_mcp_entry.py")
        os.environ["SCHOLIA_EXHAUST"] = "0"
        try:
            entry._trigger_exhaust_watcher()
        finally:
            os.environ.pop("SCHOLIA_EXHAUST", None)
        self.assertFalse(watcher.watcher_state_path(self.tempdir.name).exists())


if __name__ == "__main__":
    unittest.main()
