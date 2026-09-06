import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cc_transcript_sample.jsonl"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cc = _load("cc_exhaust", SCRIPTS / "cc_exhaust.py")
server = _load("scholialang_mcp_server", SCRIPTS / "scholialang_mcp_server.py")
tailer = _load("exhaust_tailer", SCRIPTS / "hooks" / "exhaust_tailer.py")
start_hook = _load("scholia_session_start_hook", SCRIPTS / "hooks" / "session_start.py")
end_hook = _load("scholia_session_end_hook", SCRIPTS / "hooks" / "session_end.py")


class TailerSyncTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self._saved = {k: os.environ.get(k) for k in ("SCHOLIALANG_HOME", "SCHOLIA_AUTOEMIT")}
        os.environ["SCHOLIALANG_HOME"] = self.tempdir.name
        os.environ.pop("SCHOLIA_AUTOEMIT", None)
        self.project = str(Path(self.tempdir.name) / "proj")
        Path(self.project).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tempdir.cleanup()

    def _state(self):
        info = cc.ensure_exhaust_dag(server, project_path=self.project, session_id="s")
        return {"dag_id": info["dag_id"]}

    def test_sync_state_resume_is_idempotent(self):
        state = self._state()
        state, r1 = tailer.sync_state(state, transcript_path=str(FIXTURE), project_path=self.project, max_events=2000)
        self.assertEqual(r1.appended, 10)
        self.assertEqual(state["last_line"], 10)
        state, r2 = tailer.sync_state(state, transcript_path=str(FIXTURE), project_path=self.project, max_events=2000)
        self.assertEqual(r2.appended, 0)
        self.assertEqual(state["last_line"], 10)

    def test_state_roundtrip(self):
        path = tailer.state_path(self.tempdir.name, "s")
        tailer.save_state(path, {"dag_id": "d", "last_line": 3})
        self.assertEqual(tailer.load_state(path)["last_line"], 3)

    def test_missing_transcript_is_safe(self):
        state = self._state()
        state, r = tailer.sync_state(
            state, transcript_path=str(Path(self.tempdir.name) / "nope.jsonl"),
            project_path=self.project, max_events=2000,
        )
        self.assertEqual(r.appended, 0)

    def test_sync_state_pauses_on_live_optout_and_keeps_cursor_for_resume(self):
        transcript = Path(self.tempdir.name) / "live-optout.jsonl"
        transcript.write_text(FIXTURE.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
        state = self._state()
        state, first = tailer.sync_state(
            state,
            transcript_path=str(transcript),
            project_path=self.project,
            max_events=2000,
        )
        self.assertEqual(first.appended, 1)
        self.assertEqual(state["last_line"], 1)

        (Path(self.project) / ".scholia-off").write_text("", encoding="utf-8")
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(FIXTURE.read_text(encoding="utf-8").splitlines()[1] + "\n")
        state, paused = tailer.sync_state(
            state,
            transcript_path=str(transcript),
            project_path=self.project,
            max_events=2000,
        )
        self.assertEqual(paused.appended, 0)
        self.assertEqual(state["last_line"], 1)

        (Path(self.project) / ".scholia-off").unlink()
        state, resumed = tailer.sync_state(
            state,
            transcript_path=str(transcript),
            project_path=self.project,
            max_events=2000,
        )
        self.assertEqual(resumed.appended, 1)
        self.assertEqual(state["last_line"], 2)

    def test_run_captures_then_exits_on_cap(self):
        # max_events < transcript length -> run() captures the capped set and
        # exits via the truncated break (no infinite loop, no sleep reached).
        rc = tailer.run(
            transcript_path=str(FIXTURE), project_path=self.project,
            session_id="run-1", max_events=5, poll=0.0,
        )
        self.assertEqual(rc, 0)
        info = cc.ensure_exhaust_dag(server, project_path=self.project, session_id="run-1")
        nodes = server.load_dag(info["dag_id"], self.project)["nodes"]
        ccline_ids = [nid for nid in nodes if nid.startswith("ccline_")]
        self.assertEqual(len(ccline_ids), 5)
        # State file records the resume high-water mark.
        state = tailer.load_state(tailer.state_path(self.tempdir.name, "run-1"))
        self.assertEqual(state["last_line"], 5)


class ExhaustFlagTests(unittest.TestCase):
    """SCHOLIA_EXHAUST gating in the SessionStart hook (default ON; explicit off-switch)."""

    def setUp(self):
        self._saved = os.environ.get("SCHOLIA_EXHAUST")
        os.environ.pop("SCHOLIA_EXHAUST", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SCHOLIA_EXHAUST", None)
        else:
            os.environ["SCHOLIA_EXHAUST"] = self._saved

    def test_enabled_by_default(self):
        # Default ON: exhaust runs whenever auto-emit is on (no flag needed).
        self.assertTrue(start_hook._exhaust_enabled())

    def test_enabled_values(self):
        for value in ("1", "true", "on", "YES", " On ", "", "whatever"):
            os.environ["SCHOLIA_EXHAUST"] = value
            self.assertTrue(start_hook._exhaust_enabled(), value)

    def test_disabled_values(self):
        # Only an explicit off-switch disables exhaust; launch is then skipped.
        for value in ("0", "false", "off", "no", " OFF "):
            os.environ["SCHOLIA_EXHAUST"] = value
            self.assertFalse(start_hook._exhaust_enabled(), value)
        self.assertIsNone(start_hook._maybe_launch_exhaust("/tmp", "s", None))


class SessionEndStopTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self._saved = os.environ.get("SCHOLIALANG_HOME")
        os.environ["SCHOLIALANG_HOME"] = self.tempdir.name

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SCHOLIALANG_HOME", None)
        else:
            os.environ["SCHOLIALANG_HOME"] = self._saved
        self.tempdir.cleanup()

    def test_stop_without_state_is_noop(self):
        # No state file: must not raise.
        end_hook._stop_exhaust(self.tempdir.name, "s")

    def test_stop_removes_state_file(self):
        path = tailer.state_path(self.tempdir.name, "s")
        tailer.save_state(path, {"dag_id": "d", "pid": 999999, "last_line": 1})
        self.assertTrue(Path(path).exists())
        end_hook._stop_exhaust(self.tempdir.name, "s")
        self.assertFalse(Path(path).exists())


class SessionHookLifecycleTests(unittest.TestCase):
    """The hook-announced DAG is the persisted, appendable, closed DAG."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "SCHOLIALANG_HOME",
                "SCHOLIA_AUTOEMIT",
                "SCHOLIA_EXHAUST",
                "SCHOLIA_LIVE",
                "SCHOLIA_HOST",
                "SCHOLIA_RUNTIME_ID",
            )
        }
        os.environ["SCHOLIALANG_HOME"] = self.tempdir.name
        os.environ["SCHOLIA_EXHAUST"] = "0"
        os.environ["SCHOLIA_HOST"] = "claude-code"
        os.environ["SCHOLIA_RUNTIME_ID"] = "hook-runtime"
        os.environ.pop("SCHOLIA_AUTOEMIT", None)
        os.environ.pop("SCHOLIA_LIVE", None)
        self.project = str(Path(self.tempdir.name) / "project")
        Path(self.project).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tempdir.cleanup()

    def _run_hook(self, hook, payload):
        old_stdin = hook.sys.stdin
        output = io.StringIO()
        try:
            hook.sys.stdin = io.StringIO(json.dumps(payload))
            with redirect_stdout(output):
                hook.main()
        finally:
            hook.sys.stdin = old_stdin
        return output.getvalue()

    def test_start_announces_exact_persisted_dag_and_end_closes_it(self):
        session_id = "hook-session-1"
        payload = {
            "cwd": self.project,
            "session_id": session_id,
            "reason": "test complete",
        }
        output = self._run_hook(start_hook, payload)
        match = re.search(r"(dag_[0-9TZ]+_[0-9a-f]+)", output)
        self.assertIsNotNone(match, output)
        announced_dag_id = match.group(1)

        persisted = server.load_dag(announced_dag_id, self.project)
        self.assertEqual(persisted["dag_id"], announced_dag_id)
        implicit = server.tool_dag_ensure_session(
            {"project_path": self.project}
        )["structuredContent"]
        self.assertEqual(implicit["dag_id"], announced_dag_id)
        self.assertEqual(implicit["session_id"], session_id)
        observation = server.tool_dag_add_atom(
            {
                "dag_id": announced_dag_id,
                "project_path": self.project,
                "kind": "Observation",
                "summary": "The announced DAG accepted an atom.",
            }
        )["structuredContent"]["atom"]

        self._run_hook(end_hook, payload)
        closed = server.load_dag(announced_dag_id, self.project)
        kinds = [closed["nodes"][node_id]["kind"] for node_id in closed["order"]]
        self.assertNotIn("Summary", kinds)
        self.assertEqual(kinds[-1], "Observation")
        lifecycle = closed["nodes"][closed["order"][-1]]
        self.assertIn("session ended", lifecycle["summary"].lower())
        self.assertEqual(lifecycle["attributes"], {})
        self.assertFalse(
            any(node["kind"] == "Concluding" for node in closed["nodes"].values())
        )

        after_close = server.tool_dag_ensure_session(
            {"project_path": self.project}
        )["structuredContent"]
        self.assertNotEqual(after_close["dag_id"], announced_dag_id)
        self.assertEqual(after_close["session_id"], server.RUNTIME_SESSION_ID)

    def test_hook_and_mcp_child_processes_share_real_session_binding(self):
        session_id = "cross-process-hook-session"
        payload = json.dumps({"cwd": self.project, "session_id": session_id})
        env = os.environ.copy()
        env.pop("SCHOLIA_RUNTIME_ID", None)
        started = subprocess.run(
            [sys.executable, str(SCRIPTS / "hooks" / "session_start.py")],
            input=payload,
            text=True,
            capture_output=True,
            check=True,
            env=env,
        )
        match = re.search(r"(dag_[0-9TZ]+_[0-9a-f]+)", started.stdout)
        self.assertIsNotNone(match, started.stdout)
        announced_dag_id = match.group(1)

        probe = (
            "import json, scholialang_mcp_server as s; "
            f"r=s.tool_dag_ensure_session({{'project_path': {self.project!r}}})"
            "['structuredContent']; "
            "print(json.dumps({'dag_id': r['dag_id'], 'session_id': r['session_id']}))"
        )
        implicit = subprocess.run(
            [sys.executable, "-c", probe],
            text=True,
            capture_output=True,
            check=True,
            cwd=SCRIPTS,
            env=env,
        )
        resolved = json.loads(implicit.stdout)
        self.assertEqual(resolved["dag_id"], announced_dag_id)
        self.assertEqual(resolved["session_id"], session_id)


if __name__ == "__main__":
    unittest.main()
