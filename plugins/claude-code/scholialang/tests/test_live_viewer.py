import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


webview = _load("scholialang_webview_server", SCRIPTS / "scholialang_webview_server.py")
hook = _load("scholia_session_start_hook", SCRIPTS / "hooks" / "session_start.py")


class SettingsToggleTests(unittest.TestCase):
    """The viewer's GET/POST /api/settings helpers toggle .scholia-off."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.project = self.tempdir.name
        self._saved = {k: os.environ.get(k) for k in ("SCHOLIA_AUTOEMIT", "SCHOLIA_LIVE")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tempdir.cleanup()

    def test_default_auto_emit_on(self):
        state = webview.read_settings_state(self.project)
        self.assertTrue(state["auto_emit"])
        self.assertFalse(state["marker_exists"])

    def test_disable_creates_marker(self):
        webview.set_auto_emit(self.project, False)
        self.assertTrue((Path(self.project) / ".scholia-off").exists())
        self.assertFalse(webview.read_settings_state(self.project)["auto_emit"])

    def test_enable_removes_marker(self):
        webview.set_auto_emit(self.project, False)
        webview.set_auto_emit(self.project, True)
        self.assertFalse((Path(self.project) / ".scholia-off").exists())
        self.assertTrue(webview.read_settings_state(self.project)["auto_emit"])

    def test_enable_is_idempotent_without_marker(self):
        # Enabling when no marker exists must not raise.
        webview.set_auto_emit(self.project, True)
        self.assertTrue(webview.read_settings_state(self.project)["auto_emit"])

    def test_empty_project_rejected(self):
        with self.assertRaises(ValueError):
            webview.set_auto_emit("", True)

    def test_env_disable_overrides_marker_state(self):
        os.environ["SCHOLIA_AUTOEMIT"] = "0"
        state = webview.read_settings_state(self.project)
        self.assertTrue(state["env_autoemit_disabled"])
        self.assertFalse(state["auto_emit"])


class LiveFlagTests(unittest.TestCase):
    """SCHOLIA_LIVE gating and helpers in the SessionStart hook."""

    def setUp(self):
        self._saved = os.environ.get("SCHOLIA_LIVE")
        os.environ.pop("SCHOLIA_LIVE", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SCHOLIA_LIVE", None)
        else:
            os.environ["SCHOLIA_LIVE"] = self._saved

    def test_disabled_by_default(self):
        self.assertFalse(hook._live_enabled())
        self.assertIsNone(hook._maybe_launch_live("/tmp"))

    def test_enabled_values(self):
        for value in ("1", "true", "on", "YES", " On "):
            os.environ["SCHOLIA_LIVE"] = value
            self.assertTrue(hook._live_enabled(), value)

    def test_disabled_values(self):
        for value in ("0", "false", "off", "no", ""):
            os.environ["SCHOLIA_LIVE"] = value
            self.assertFalse(hook._live_enabled(), value)

    def test_free_port_returns_int(self):
        self.assertIsInstance(hook._free_port(8765), int)


if __name__ == "__main__":
    unittest.main()
