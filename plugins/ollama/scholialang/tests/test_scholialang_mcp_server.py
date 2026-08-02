import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


SERVER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "scholialang_mcp_server.py"
SPEC = importlib.util.spec_from_file_location("scholialang_mcp_server", SERVER_PATH)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class ScholialangDagTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("SCHOLIALANG_HOME")
        os.environ["SCHOLIALANG_HOME"] = self.tempdir.name
        self.project_path = str(Path(self.tempdir.name) / "project")

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("SCHOLIALANG_HOME", None)
        else:
            os.environ["SCHOLIALANG_HOME"] = self.old_home
        self.tempdir.cleanup()

    def start_dag(self):
        result = server.tool_dag_start(
            {
                "project_path": self.project_path,
                "title": "Test DAG",
                "objective": "Verify graph-first trace storage.",
                "tags": ["test"],
            }
        )
        return result["structuredContent"]["dag_id"]

    def add_atom(self, dag_id, kind, summary, links=None):
        result = server.tool_dag_add_atom(
            {
                "dag_id": dag_id,
                "project_path": self.project_path,
                "kind": kind,
                "summary": summary,
                "links": links or [],
            }
        )
        return result["structuredContent"]["atom"]["id"]

    def test_add_atoms_and_frontier(self):
        dag_id = self.start_dag()
        hypothesis = self.add_atom(dag_id, "Hypothesis", "Generated files may return.")
        observation = self.add_atom(dag_id, "Observation", "Merge tree excludes generated files.")
        evidence = self.add_atom(
            dag_id,
            "Evidence",
            "Observation refutes the generated-file hypothesis.",
            [{"to": hypothesis, "relation": "refutes"}, {"to": observation, "relation": "derived_from"}],
        )
        finding = self.add_atom(
            dag_id,
            "Finding",
            "Generated-file risk is refuted.",
            [{"to": evidence, "relation": "derived_from"}],
        )

        frontier = server.tool_dag_frontier({"dag_id": dag_id, "project_path": self.project_path})
        frontier_ids = [node["id"] for node in frontier["structuredContent"]["frontier"]]
        self.assertIn(finding, frontier_ids)
        self.assertNotIn(hypothesis, frontier_ids)

        summary = server.tool_dag_summary({"dag_id": dag_id, "project_path": self.project_path})
        self.assertIn("Generated-file risk is refuted", summary["content"][0]["text"])

        db_path = Path(self.tempdir.name) / "scholialang.sqlite3"
        self.assertTrue(db_path.exists())
        conn = sqlite3.connect(db_path)
        try:
            node_count = conn.execute("SELECT COUNT(*) FROM nodes WHERE dag_id = ?", (dag_id,)).fetchone()[0]
            edge_count = conn.execute("SELECT COUNT(*) FROM edges WHERE dag_id = ?", (dag_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(node_count, 5)
        self.assertEqual(edge_count, 3)

    def test_catalog_and_lint_support_goal_and_concluding(self):
        catalog = server.tool_catalog({})["structuredContent"]
        tags = {item["tag"] for item in catalog["atoms"]}
        self.assertIn("Goal", tags)
        self.assertIn("Concluding", tags)

        result = server.tool_lint_snippet(
            {
                "snippet": (
                    '<Goal id="Goal_01" priority="required">ship the fix</Goal>'
                    '<Observation id="Obs_01">ship checks passed</Observation>'
                    '<Concluding id="Concluding_01" for_goal="Goal_01">'
                    'REFER:Obs_01 done'
                    '</Concluding>'
                )
            }
        )["structuredContent"]
        self.assertTrue(result["ok"], result)

    def test_cycle_is_rejected(self):
        dag_id = self.start_dag()
        first = self.add_atom(dag_id, "Hypothesis", "A")
        second = self.add_atom(dag_id, "Finding", "B", [{"to": first, "relation": "derived_from"}])

        with self.assertRaises(ValueError):
            server.tool_dag_link(
                {
                    "dag_id": dag_id,
                    "project_path": self.project_path,
                    "from": first,
                    "to": second,
                    "relation": "derived_from",
                }
            )

    def test_trace_aliases_use_dag_store(self):
        started = server.TOOLS["scholia.trace_start"]({"project_path": self.project_path, "title": "Alias"})
        trace_id = started["structuredContent"]["trace_id"]
        appended = server.TOOLS["scholia.trace_append"](
            {
                "trace_id": trace_id,
                "project_path": self.project_path,
                "kind": "Finding",
                "summary": "Trace alias writes a DAG node.",
            }
        )
        self.assertEqual(appended["structuredContent"]["dag_id"], trace_id)

        read = server.TOOLS["scholia.trace_read"](
            {
                "trace_id": trace_id,
                "project_path": self.project_path,
                "include_nodes": True,
            }
        )
        self.assertEqual(read["structuredContent"]["dag"]["node_count"], 2)

    def test_json_rpc_tools_list_includes_dag_tools(self):
        response = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertIn("scholia_dag_start", names)
        self.assertIn("scholia_dag_frontier", names)
        self.assertIn("scholia_codex_import_thread", names)
        self.assertNotIn("scholia.dag_start", names)

    def test_initialize_negotiates_supported_protocol_version(self):
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "codex", "version": "test"},
                },
            }
        )
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")

    def test_tool_schemas_avoid_type_arrays(self):
        response = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        def walk(value):
            if isinstance(value, dict):
                self.assertFalse(isinstance(value.get("type"), list), value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        for tool in response["result"]["tools"]:
            walk(tool["inputSchema"])

    def test_codex_import_thread_builds_exhaust_dag(self):
        rollout_path = Path(self.tempdir.name) / "rollout.jsonl"
        events = [
            {
                "timestamp": "2026-05-29T00:00:00.000Z",
                "type": "session_meta",
                "payload": {"id": "thread_01", "cwd": self.project_path, "base_instructions": {"text": "do not copy by default"}},
            },
            {
                "timestamp": "2026-05-29T00:00:01.000Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Analyze the site exhaustively.", "images": []},
            },
            {
                "timestamp": "2026-05-29T00:00:02.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "pwd"}),
                    "call_id": "call_01",
                },
            },
            {
                "timestamp": "2026-05-29T00:00:03.000Z",
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "call_01", "output": "workspace\n"},
            },
            {
                "timestamp": "2026-05-29T00:00:04.000Z",
                "type": "response_item",
                "payload": {"type": "reasoning", "encrypted_content": "encrypted-private-thoughts"},
            },
            {
                "timestamp": "2026-05-29T00:00:05.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "input": "*** Begin Patch\n*** End Patch\n",
                    "call_id": "call_02",
                },
            },
            {
                "timestamp": "2026-05-29T00:00:06.000Z",
                "type": "event_msg",
                "payload": {"type": "patch_apply_end", "call_id": "call_02", "stdout": "Success", "stderr": "", "changes": {}},
            },
        ]
        rollout_path.write_text("\n".join(json.dumps(event) for event in events))

        result = server.tool_codex_import_thread(
            {
                "project_path": self.project_path,
                "rollout_path": str(rollout_path),
                "max_content_chars": 200,
            }
        )
        structured = result["structuredContent"]
        dag_id = structured["dag_id"]
        self.assertEqual(structured["events_imported"], 7)
        self.assertEqual(structured["canonical_counts"]["task_tool_call"], 2)
        self.assertEqual(structured["canonical_counts"]["task_tool_result"], 2)
        self.assertEqual(structured["canonical_counts"]["task_message"], 1)

        read = server.tool_dag_read(
            {
                "dag_id": dag_id,
                "project_path": self.project_path,
                "include_nodes": True,
                "include_edges": True,
                "limit": 40,
            }
        )["structuredContent"]
        summaries = [node["summary"] for node in read["nodes"]]
        kinds = [node["kind"] for node in read["nodes"]]
        content = "\n".join(node.get("content", "") for node in read["nodes"])
        self.assertIn("Goal", kinds)
        self.assertIn("Concluding", kinds)
        self.assertTrue(any("captures user prompt" in summary for summary in summaries))
        self.assertTrue(any("Codex canonical event" in summary and "task_tool_call" in summary for summary in summaries))
        self.assertIn('"event": "task_tool_result"', content)
        self.assertIn("Analyze the site exhaustively.", content)
        self.assertIn("encrypted_content", content)
        self.assertIn("text_omitted_reason", content)
        self.assertNotIn("encrypted-private-thoughts", content)
        self.assertTrue(any("custom_tool_call calls apply_patch" in summary for summary in summaries))
        self.assertTrue(any("patch_apply_end completed apply_patch" in summary for summary in summaries))
        self.assertTrue(
            any(edge["relation"] == "derived_from" and edge.get("label") == "tool output for call_id" for edge in read["edges"])
        )
        self.assertTrue(
            any(
                edge["relation"] == "derived_from" and edge.get("label") == "canonical tool result for tool_use_id"
                for edge in read["edges"]
            )
        )
        self.assertTrue(
            any(edge["relation"] == "derived_from" and edge.get("label") == "for_goal status=met" for edge in read["edges"])
        )

    def test_codex_import_thread_prefers_explicit_rollout_thread_metadata(self):
        rollout_path = Path(self.tempdir.name) / "target-rollout.jsonl"
        latest_rollout_path = Path(self.tempdir.name) / "latest-rollout.jsonl"
        rollout_path.write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-29T00:00:00.000Z",
                    "type": "session_meta",
                    "payload": {"id": "target_thread", "cwd": self.project_path},
                }
            )
        )
        latest_rollout_path.write_text("{}")

        codex_home = Path(self.tempdir.name) / "codex-home"
        codex_home.mkdir()
        conn = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            conn.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    title TEXT NOT NULL,
                    archived INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO threads
                (id, rollout_path, cwd, title, archived, updated_at, updated_at_ms)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                [
                    ("target_thread", str(rollout_path), self.project_path, "Target migration", 100, 100000),
                    ("latest_thread", str(latest_rollout_path), self.project_path, "Latest unrelated", 200, 200000),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        result = server.tool_codex_import_thread(
            {
                "codex_home": str(codex_home),
                "project_path": self.project_path,
                "rollout_path": str(rollout_path),
                "include_canonical_events": False,
                "max_content_chars": 200,
            }
        )["structuredContent"]

        self.assertEqual(result["thread_id"], "target_thread")
        read = server.tool_dag_read(
            {
                "dag_id": result["dag_id"],
                "project_path": self.project_path,
                "include_nodes": True,
                "limit": 5,
            }
        )["structuredContent"]
        self.assertEqual(read["dag"]["title"], "Codex exhaust: Target migration")
        content = "\n".join(node.get("content", "") for node in read["nodes"])
        self.assertIn('"thread_id": "target_thread"', content)
        self.assertNotIn("latest_thread", content)

    def test_codex_import_thread_normalizes_internal_agent_harness_cli_stream(self):
        rollout_path = Path(self.tempdir.name) / "codex-cli.jsonl"
        events = [
            {"type": "thread.started", "thread_id": "thread_cli"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "item_msg", "type": "agent_message", "text": "I will inspect the repo."}},
            {"type": "item.completed", "item": {"id": "item_tool", "type": "tool_use", "name": "bash", "input": {"command": "pwd"}}},
            {"type": "item.completed", "item": {"id": "item_result", "type": "tool_result", "tool_use_id": "item_tool", "output": "workspace\n"}},
            {
                "type": "item.completed",
                "item": {
                    "id": "item_command",
                    "type": "command_execution",
                    "command": "ls",
                    "aggregated_output": "README.md\n",
                    "exit_code": 0,
                },
            },
            {"type": "turn.completed", "usage": {"input_tokens": 11, "cached_input_tokens": 3, "output_tokens": 7}},
        ]
        rollout_path.write_text("\n".join(json.dumps(event) for event in events))

        result = server.tool_codex_import_thread(
            {
                "project_path": self.project_path,
                "rollout_path": str(rollout_path),
                "run_id": "run_cli",
                "task_id": "task_cli",
                "max_content_chars": 200,
            }
        )
        structured = result["structuredContent"]
        self.assertEqual(structured["events_imported"], 7)
        self.assertEqual(structured["canonical_counts"]["task_message"], 1)
        self.assertEqual(structured["canonical_counts"]["task_tool_call"], 2)
        self.assertEqual(structured["canonical_counts"]["task_tool_result"], 2)
        self.assertEqual(structured["canonical_counts"]["token_usage"], 1)
        self.assertEqual(structured["canonical_counts"]["task_output"], 3)

        read = server.tool_dag_read(
            {
                "dag_id": structured["dag_id"],
                "project_path": self.project_path,
                "include_nodes": True,
                "include_edges": True,
                "limit": 60,
            }
        )["structuredContent"]
        content = "\n".join(node.get("content", "") for node in read["nodes"])
        self.assertIn('"event": "task_tool_call"', content)
        self.assertIn('"event": "task_tool_result"', content)
        self.assertIn('"event": "token_usage"', content)
        self.assertIn('"tool": "bash"', content)
        self.assertIn('"command": "ls"', content)
        self.assertIn('"cache_read_input_tokens": 3', content)
        self.assertTrue(
            any(
                edge["relation"] == "derived_from" and edge.get("label") == "canonical tool result for tool_use_id"
                for edge in read["edges"]
            )
        )


class ScholialangValidatorTests(unittest.TestCase):
    """Coverage for the full v0.6 grammar validator surface.

    These tests assert behaviour, not the internal engine path. The lint
    surface should give the same answers whether driven by the installed
    scholialang package or the vendored snapshot — only the
    ``lint_engine`` field changes.
    """

    def test_lint_engine_resolves(self):
        self.assertIn(
            server.LINT_ENGINE,
            {"scholialang-package", "scholialang-vendored"},
        )

    def test_lint_good_trace_passes(self):
        snippet = '<Step id="S1"><Goal id="G1">Solve the bug.</Goal></Step>'
        result = json.loads(server.tool_lint_snippet({"snippet": snippet})["content"][0]["text"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["warnings"], [])

    def test_lint_open_hypothesis_violates_rule(self):
        snippet = '<Step id="S1"><Goal id="G1">x</Goal><Hypothesis id="H1">y</Hypothesis></Step>'
        result = json.loads(server.tool_lint_snippet({"snippet": snippet})["content"][0]["text"])
        self.assertFalse(result["ok"])
        rules = {err["rule"] for err in result["errors"]}
        self.assertIn("hypothesis_evaluated", rules)

    def test_lint_unrecorded_action_violates_rule(self):
        snippet = '<Step id="S1"><Goal id="G1">x</Goal><Action id="A1">do it</Action></Step>'
        result = json.loads(server.tool_lint_snippet({"snippet": snippet})["content"][0]["text"])
        rules = {err["rule"] for err in result["errors"]}
        self.assertIn("action_recorded", rules)

    def test_lint_tag_balance_back_compat(self):
        snippet = "<Foo><Bar></Foo>"
        result = json.loads(
            server.tool_lint_snippet({"snippet": snippet, "mode": "tag_balance"})["content"][0]["text"]
        )
        self.assertEqual(result["mode"], "tag_balance")
        self.assertEqual(result["lint_engine"], "tag-balance-only")
        self.assertFalse(result["ok"])
        self.assertTrue(any("Bar" in err["message"] or "Foo" in err["message"] for err in result["errors"]))

    def test_lint_malformed_snippet_returns_well_formed_error(self):
        snippet = "<Step><Goal>unclosed"
        result = json.loads(server.tool_lint_snippet({"snippet": snippet})["content"][0]["text"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["rule"], "well_formed")

    def test_lint_trace_returns_per_rule_breakdown(self):
        snippet = '<Step id="S1"><Hypothesis id="H1">y</Hypothesis></Step>'
        result = json.loads(server.tool_lint_trace({"snippet": snippet})["content"][0]["text"])
        self.assertFalse(result["ok"])
        self.assertIn("errors_by_rule", result)
        self.assertIn("warnings_by_rule", result)
        self.assertIn("counts_by_rule", result)
        self.assertIn("warning_counts_by_rule", result)
        self.assertIn("rules", result)
        self.assertIn("hypothesis_evaluated", result["rules"])
        self.assertIn("well_formed", result["rules"])
        self.assertGreater(result["counts_by_rule"]["hypothesis_evaluated"], 0)

    def test_lint_concluding_warning_is_non_fatal(self):
        snippet = (
            '<Step id="S1">'
            '<Goal id="G_01" priority="required">g</Goal>'
            '<Hypothesis id="H_01">h</Hypothesis>'
            '<Evidence id="E_01" for="H_01" polarity="supports">e</Evidence>'
            '<Finding id="F_01" for_hyp="H_01" status="met">f</Finding>'
            '<Concluding id="C_01" for_goal="G_01">REFER:F_01 says we should ship.</Concluding>'
            '</Step>'
        )
        result = json.loads(server.tool_lint_snippet({"snippet": snippet})["content"][0]["text"])
        self.assertTrue(result["ok"], result)
        warning_rules = {warning["rule"] for warning in result["warnings"]}
        self.assertIn("no_action_in_concluding", warning_rules)

    def test_catalog_exposes_v05_closed_sets(self):
        result = json.loads(server.tool_catalog({})["content"][0]["text"])
        self.assertIn("scholia_atom_kinds_v05", result)
        self.assertEqual(len(result["scholia_atom_kinds_v05"]), 32)
        self.assertIn("Concluding", result["scholia_atom_kinds_v05"])
        self.assertIn("scholia_canonical_operators_v05", result)
        self.assertIn("scholia_criticality_rank", result)
        self.assertEqual(result["scholia_validator_version"], "0.7.1")
        # Back-compat aliases remain available for older clients.
        self.assertIn("scholia_atom_kinds_v04", result)
        self.assertIn("scholia_canonical_operators_v04", result)
        self.assertIn(result["lint_engine"], {"scholialang-package", "scholialang-vendored"})

    def test_lint_tools_registered_in_tools_list(self):
        names = {t["name"] for t in server.list_tools()}
        self.assertIn("scholia_lint_snippet", names)
        self.assertIn("scholia_lint_trace", names)

    def test_lint_snippet_schema_accepts_mode(self):
        schema = server.tool_schema("scholia_lint_snippet")
        self.assertIn("mode", schema["properties"])
        self.assertIn("snippet", schema["required"])


class ScholialangPluginManifestTests(unittest.TestCase):
    """Smoke tests on the sibling plugin trees' manifests.

    Catches malformed JSON, missing required fields, or path drift before
    a release tag is cut. Every plugin's tests dir gets a copy of this
    class, so any one plugin's test run validates the whole tree.
    """

    REPO_ROOT = SERVER_PATH.resolve().parents[4]

    def _read_json(self, *parts):
        path = self.REPO_ROOT.joinpath(*parts)
        self.assertTrue(path.exists(), f"missing manifest: {path}")
        return json.loads(path.read_text())

    def test_marketplace_lists_three_plugins(self):
        data = self._read_json(".agents", "plugins", "marketplace.json")
        names = {p["name"] for p in data["plugins"]}
        self.assertIn("scholialang", names)
        self.assertIn("scholialang-claude-code", names)
        self.assertIn("scholialang-ollama", names)

    def test_codex_plugin_manifest_well_formed(self):
        data = self._read_json("plugins", "codex", "scholialang", ".codex-plugin", "plugin.json")
        self.assertEqual(data["name"], "scholialang")
        self.assertIn("license", data)
        self.assertIn("mcpServers", data)

    def test_claude_code_plugin_manifest_well_formed(self):
        data = self._read_json("plugins", "claude-code", "scholialang", ".claude-plugin", "plugin.json")
        self.assertEqual(data["name"], "scholialang")
        self.assertIn("license", data)
        self.assertIn("mcpServers", data)

    def test_claude_code_mcp_config_points_at_server(self):
        data = self._read_json("plugins", "claude-code", "scholialang", ".mcp.json")
        self.assertIn("scholialang", data["mcpServers"])
        args = data["mcpServers"]["scholialang"]["args"]
        self.assertTrue(any("scholialang_mcp_server.py" in arg for arg in args))

    def test_ollama_recipes_present(self):
        recipes_dir = self.REPO_ROOT / "plugins" / "ollama" / "scholialang" / "recipes"
        self.assertTrue(recipes_dir.is_dir())
        files = {p.name for p in recipes_dir.iterdir()}
        self.assertIn("continue-config.snippet.yaml", files)
        self.assertIn("cline-mcp.snippet.json", files)
        self.assertIn("open-webui-mcp.snippet.json", files)
        self.assertIn("generic-stdio.md", files)

    def test_ollama_cline_recipe_is_valid_json(self):
        path = self.REPO_ROOT / "plugins" / "ollama" / "scholialang" / "recipes" / "cline-mcp.snippet.json"
        data = json.loads(path.read_text())
        self.assertIn("mcpServers", data)
        self.assertIn("scholialang", data["mcpServers"])

    def test_all_plugin_servers_share_same_validator_engine(self):
        codex_server = self.REPO_ROOT / "plugins" / "codex" / "scholialang" / "scripts" / "scholialang_mcp_server.py"
        cc_server = self.REPO_ROOT / "plugins" / "claude-code" / "scholialang" / "scripts" / "scholialang_mcp_server.py"
        ollama_server = self.REPO_ROOT / "plugins" / "ollama" / "scholialang" / "scripts" / "scholialang_mcp_server.py"
        for path in (codex_server, cc_server, ollama_server):
            self.assertTrue(path.exists(), f"missing server: {path}")
        self.assertEqual(
            cc_server.read_text(),
            codex_server.read_text(),
            "Claude Code plugin server drifted from the Codex plugin server.",
        )
        self.assertEqual(
            ollama_server.read_text(),
            codex_server.read_text(),
            "Ollama plugin server drifted from the Codex plugin server.",
        )


if __name__ == "__main__":
    unittest.main()
