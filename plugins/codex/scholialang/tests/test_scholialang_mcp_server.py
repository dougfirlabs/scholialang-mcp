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
        self.assertEqual(node_count, 4)
        self.assertEqual(edge_count, 3)

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

    def test_html_export_can_write_quiet_trace_viewer(self):
        dag_id = self.start_dag()
        observation = self.add_atom(dag_id, "Observation", "Command output captured.")
        self.add_atom(
            dag_id,
            "Finding",
            "The exported trace is readable.",
            [{"to": observation, "relation": "derived_from"}],
        )

        result = server.tool_dag_export(
            {
                "dag_id": dag_id,
                "project_path": self.project_path,
                "format": "html",
                "write_file": True,
                "include_trace_link": False,
            }
        )
        structured = result["structuredContent"]
        export_path = Path(structured["export_path"])
        content = result["content"][0]["text"]
        html_text = export_path.read_text(encoding="utf-8")

        self.assertEqual(structured["format"], "html")
        self.assertTrue(export_path.exists())
        self.assertNotIn(str(export_path), content)
        self.assertIn("<!doctype html>", html_text)
        self.assertIn('id="q"', html_text)
        self.assertIn('id="srml-view"', html_text)
        self.assertIn("Full SRML", html_text)
        self.assertIn('class="srml-tag">Trace', html_text)
        self.assertIn('class="srml-attr">id', html_text)
        self.assertIn("Command output captured.", html_text)
        self.assertIn("The exported trace is readable.", html_text)

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
        self.assertEqual(read["structuredContent"]["dag"]["node_count"], 1)

    def test_json_rpc_tools_list_includes_dag_tools(self):
        response = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertIn("scholia.dag_start", names)
        self.assertIn("scholia.dag_frontier", names)
        self.assertIn("scholia.codex_import_thread", names)

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
        content = "\n".join(node.get("content", "") for node in read["nodes"])
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

    def test_codex_import_thread_normalizes_opentalon_cli_stream(self):
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


if __name__ == "__main__":
    unittest.main()
