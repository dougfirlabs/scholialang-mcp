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


class ProjectPathResolutionTests(unittest.TestCase):
    """project_path() resolution distinguishes absent / present-empty / set."""

    def test_absent_falls_back_to_default(self):
        self.assertEqual(webview.resolve_project_path({}, "/launch"), "/launch")

    def test_present_empty_resolves_to_none(self):
        # parse_qs(keep_blank_values=True) yields {"project_path": [""]} for
        # `?project_path=` — the blended all-projects view.
        self.assertIsNone(webview.resolve_project_path({"project_path": [""]}, "/launch"))

    def test_present_value_resolves_to_that_path(self):
        self.assertEqual(
            webview.resolve_project_path({"project_path": ["/a/b"]}, "/launch"),
            "/a/b",
        )

    def test_absent_default_none_is_preserved(self):
        self.assertIsNone(webview.resolve_project_path({}, None))


class BuildProjectsTests(unittest.TestCase):
    """build_projects() groups, sorts, and flags recently-active projects."""

    NOW = "2026-06-15T12:00:00Z"

    def _dag(self, key, path, name, updated, session="host:s1"):
        return {
            "project_key": key,
            "project_path": path,
            "project_name": name,
            "session_key": session,
            "updated_at": updated,
        }

    def test_groups_by_project_and_sorts_descending(self):
        dags = [
            self._dag("k1", "/a", "A", "2026-06-15T11:59:00Z"),
            self._dag("k1", "/a", "A", "2026-06-15T11:30:00Z"),
            self._dag("k2", "/b", "B", "2026-06-15T11:50:00Z"),
        ]
        projects = webview.build_projects(dags, self.NOW, 300)
        self.assertEqual([p["project_name"] for p in projects], ["A", "B"])
        by_name = {p["project_name"]: p for p in projects}
        self.assertEqual(by_name["A"]["dag_count"], 2)
        self.assertEqual(by_name["A"]["last_updated"], "2026-06-15T11:59:00Z")

    def test_live_boundary(self):
        # 11:55:00Z is exactly 300s before NOW.
        dags = [self._dag("kx", "/x", "X", "2026-06-15T11:55:00Z")]
        self.assertTrue(webview.build_projects(dags, self.NOW, 300)[0]["live"])
        self.assertFalse(webview.build_projects(dags, self.NOW, 299)[0]["live"])

    def test_liveness_counts_session_dags_only(self):
        # A recent DAG with no session_key does not make the project live.
        dags = [self._dag("kn", "/n", "N", self.NOW, session=None)]
        self.assertFalse(webview.build_projects(dags, self.NOW, 300)[0]["live"])

    def test_global_none_path_handled(self):
        dags = [self._dag("global", None, None, "2026-06-15T10:00:00Z", session=None)]
        projects = webview.build_projects(dags, self.NOW, 300)
        self.assertEqual(projects[0]["project_name"], "Global")
        self.assertIsNone(projects[0]["project_path"])
        self.assertFalse(projects[0]["live"])

    def test_empty_input_returns_empty(self):
        self.assertEqual(webview.build_projects([], self.NOW, 300), [])


class ScopePrecedenceTests(unittest.TestCase):
    """resolve_scope() precedence and the SCHOLIA_LIVE_SCOPE/RECENT env reads."""

    def setUp(self):
        self._saved = {
            k: os.environ.get(k)
            for k in ("SCHOLIA_LIVE_SCOPE", "SCHOLIA_LIVE_RECENT_SECS")
        }
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_url_scope_wins(self):
        self.assertEqual(webview.resolve_scope("all", "project", "project"), "all")

    def test_saved_choice_wins_over_env(self):
        self.assertEqual(webview.resolve_scope(None, "all", "project"), "all")

    def test_env_wins_over_builtin_default(self):
        self.assertEqual(webview.resolve_scope(None, None, "all"), "all")

    def test_builtin_default_is_project(self):
        self.assertEqual(webview.resolve_scope(None, None, None), "project")

    def test_invalid_values_ignored_per_tier(self):
        self.assertEqual(webview.resolve_scope("nonsense", "", "all"), "all")

    def test_env_default_scope_reads_env(self):
        self.assertEqual(webview.env_default_scope(), "project")
        os.environ["SCHOLIA_LIVE_SCOPE"] = "all"
        self.assertEqual(webview.env_default_scope(), "all")
        os.environ["SCHOLIA_LIVE_SCOPE"] = "garbage"
        self.assertEqual(webview.env_default_scope(), "project")

    def test_recent_window_secs_default_and_override(self):
        self.assertEqual(webview.recent_window_secs(), 300)
        os.environ["SCHOLIA_LIVE_RECENT_SECS"] = "600"
        self.assertEqual(webview.recent_window_secs(), 600)
        os.environ["SCHOLIA_LIVE_RECENT_SECS"] = "not-a-number"
        self.assertEqual(webview.recent_window_secs(), 300)

    def test_env_all_opens_fresh_tab_in_all_scope(self):
        # Story 3 AC1: SCHOLIA_LIVE_SCOPE=all + no URL/localStorage choice -> all.
        os.environ["SCHOLIA_LIVE_SCOPE"] = "all"
        self.assertEqual(
            webview.resolve_scope(None, None, webview.env_default_scope()),
            "all",
        )


if __name__ == "__main__":
    unittest.main()
