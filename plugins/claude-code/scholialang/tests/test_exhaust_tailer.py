import importlib.util
import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
