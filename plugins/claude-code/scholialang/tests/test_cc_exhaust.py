import importlib.util
import json
import os
import socket
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
webview = _load("scholialang_webview_server", SCRIPTS / "scholialang_webview_server.py")


def _lines():
    return FIXTURE.read_text().splitlines()


class ParserTests(unittest.TestCase):
    """The pure transcript parser maps each record to one exhaust atom."""

    def test_maps_record_types_to_atom_kinds(self):
        result = cc.parse_transcript_lines(_lines(), source="cc.jsonl")
        kinds = [a["kind"] for a in result.atoms]
        self.assertEqual(
            kinds,
            [
                "Observation",   # summary record (Summary is not an atom)
                "Question",      # user string prompt
                "Finding",       # assistant thinking + text
                "Action",        # assistant tool_use
                "Observation",   # user tool_result
                "Finding",       # assistant text
                "Observation",   # attachment (generic)
                "Finding",       # assistant text (with secret)
                "Observation",   # last-prompt (generic)
                "Contradiction", # malformed JSON line
            ],
        )

    def test_one_atom_per_nonblank_line(self):
        result = cc.parse_transcript_lines(_lines(), source="cc.jsonl")
        self.assertEqual(len(result.atoms), 10)

    def test_stable_per_line_ids(self):
        result = cc.parse_transcript_lines(_lines(), source="cc.jsonl")
        # atom id encodes the (1-based) line number, padded and stable.
        self.assertEqual(result.atoms[0]["atom_id"], cc.atom_id_for(1))
        self.assertEqual(result.atoms[2]["atom_id"], cc.atom_id_for(3))
        self.assertEqual(result.atoms[0]["line"], 1)
        for atom in result.atoms:
            self.assertEqual(atom["atom_id"], cc.atom_id_for(atom["line"]))

    def test_reparse_is_idempotent_by_line_number(self):
        first = cc.parse_transcript_lines(_lines(), source="cc.jsonl")
        second = cc.parse_transcript_lines(_lines(), source="cc.jsonl")
        self.assertEqual(
            [a["atom_id"] for a in first.atoms],
            [a["atom_id"] for a in second.atoms],
        )
        ids = [a["atom_id"] for a in first.atoms]
        self.assertEqual(len(ids), len(set(ids)))  # no duplicate ids in one parse

    def test_max_events_cap_and_truncation_flag(self):
        result = cc.parse_transcript_lines(_lines(), max_events=3, source="cc.jsonl")
        self.assertEqual(len(result.atoms), 3)
        self.assertTrue(result.truncated)
        self.assertEqual(result.scanned, 3)

    def test_secret_is_scrubbed_from_content(self):
        result = cc.parse_transcript_lines(_lines(), source="cc.jsonl")
        joined = "\n".join(a["content"] for a in result.atoms)
        self.assertNotIn("sk-do-not-capture-this-secret-0001", joined)

    def test_resume_from_start_line(self):
        result = cc.parse_transcript_lines(_lines(), start_line=6, source="cc.jsonl")
        self.assertEqual([a["line"] for a in result.atoms], [6, 7, 8, 9, 10])


class CaptureTests(unittest.TestCase):
    """End-to-end capture into a real (temp) exhaust DAG."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self._saved = {k: os.environ.get(k) for k in ("SCHOLIALANG_HOME", "SCHOLIA_AUTOEMIT", "SCHOLIA_EXHAUST")}
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

    def _exhaust_dag(self):
        info = cc.ensure_exhaust_dag(server, project_path=self.project, session_id="sess-1")
        self.assertIsNotNone(info)
        return info["dag_id"]

    def test_capture_appends_atoms(self):
        dag_id = self._exhaust_dag()
        res = cc.capture_once(server, transcript_path=str(FIXTURE), dag_id=dag_id, project_path=self.project, max_events=2000)
        self.assertEqual(res.appended, 10)

    def test_capture_is_idempotent(self):
        dag_id = self._exhaust_dag()
        cc.capture_once(server, transcript_path=str(FIXTURE), dag_id=dag_id, project_path=self.project, max_events=2000)
        before = len(server.load_dag(dag_id, self.project)["nodes"])
        res = cc.capture_once(server, transcript_path=str(FIXTURE), dag_id=dag_id, project_path=self.project, max_events=2000)
        after = len(server.load_dag(dag_id, self.project)["nodes"])
        self.assertEqual(res.appended, 0)
        self.assertEqual(before, after)

    def test_capture_resume_does_not_duplicate(self):
        dag_id = self._exhaust_dag()
        first = cc.capture_once(server, transcript_path=str(FIXTURE), dag_id=dag_id, project_path=self.project, max_events=5)
        self.assertEqual(first.appended, 5)
        self.assertTrue(first.truncated)
        # Raise the cap and resume from where we left off.
        second = cc.capture_once(
            server, transcript_path=str(FIXTURE), dag_id=dag_id, project_path=self.project,
            max_events=2000, start_line=first.last_line + 1,
        )
        self.assertEqual(second.appended, 5)
        nodes = server.load_dag(dag_id, self.project)["nodes"]
        ccline_ids = [nid for nid in nodes if nid.startswith("ccline_")]
        self.assertEqual(len(ccline_ids), 10)

    def test_trailing_partial_record_is_retried_after_newline_completion(self):
        dag_id = self._exhaust_dag()
        transcript = Path(self.tempdir.name) / "partial-transcript.jsonl"
        second = _lines()[1].encode("utf-8")
        split_at = len(second) // 2
        transcript.write_bytes(_lines()[0].encode("utf-8") + b"\n" + second[:split_at])

        first = cc.capture_once(
            server,
            transcript_path=str(transcript),
            dag_id=dag_id,
            project_path=self.project,
        )
        self.assertEqual(first.appended, 1)
        self.assertEqual(first.last_line, 1)

        with transcript.open("ab") as stream:
            stream.write(second[split_at:] + b"\n")
        second_pass = cc.capture_once(
            server,
            transcript_path=str(transcript),
            dag_id=dag_id,
            project_path=self.project,
            start_line=first.last_line + 1,
            previous_atom_id=first.last_atom_id,
        )
        self.assertEqual(second_pass.appended, 1)
        self.assertEqual(second_pass.last_line, 2)
        nodes = server.load_dag(dag_id, self.project)["nodes"]
        self.assertNotIn("JSON parse error", nodes[cc.atom_id_for(2)]["summary"])

    def test_truncation_is_logged(self):
        dag_id = self._exhaust_dag()
        logged = []
        cc.capture_once(
            server, transcript_path=str(FIXTURE), dag_id=dag_id, project_path=self.project,
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
            res = cc.capture_once(server, transcript_path=str(FIXTURE), dag_id=dag_id, project_path=self.project, max_events=2000)
        finally:
            socket.socket = original
        self.assertEqual(res.appended, 10)


class PairingTests(unittest.TestCase):
    """The exhaust DAG title-matches and view-mode-pairs the checkpoint DAG."""

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

    def _meta(self, dag_id):
        return webview.enrich_dag_metadata(server.load_dag(dag_id, self.project))

    def test_exhaust_view_mode_and_match_score(self):
        checkpoint = server.tool_dag_ensure_session(
            {"project_path": self.project, "session_id": "sess-1", "host": "claude-code", "auto": True}
        )["structuredContent"]
        info = cc.ensure_exhaust_dag(server, project_path=self.project, session_id="sess-1")

        cp_meta = self._meta(checkpoint["dag_id"])
        ex_meta = self._meta(info["dag_id"])
        self.assertEqual(webview.trace_view_mode(cp_meta), "checkpoint")
        self.assertEqual(webview.trace_view_mode(ex_meta), "exhaust")
        self.assertGreaterEqual(webview.trace_match_score(cp_meta, ex_meta), 42)

    def test_related_trace_views_pairs_both(self):
        checkpoint = server.tool_dag_ensure_session(
            {"project_path": self.project, "session_id": "sess-1", "host": "claude-code", "auto": True}
        )["structuredContent"]
        info = cc.ensure_exhaust_dag(server, project_path=self.project, session_id="sess-1")
        dags = [self._meta(d["dag_id"]) for d in server.all_dags(self.project)]
        cp_meta = self._meta(checkpoint["dag_id"])
        views = webview.related_trace_views(cp_meta, dags)
        self.assertIsNotNone(views["checkpoint"])
        self.assertIsNotNone(views["exhaust"])
        self.assertEqual(views["exhaust"]["dag_id"], info["dag_id"])

    def test_opt_out_suppresses_exhaust_dag(self):
        (Path(self.project) / ".scholia-off").write_text("")
        info = cc.ensure_exhaust_dag(server, project_path=self.project, session_id="sess-1")
        self.assertIsNone(info)


class JsonlBufferingTests(unittest.TestCase):
    """A poll can land midway through a multibyte UTF-8 sequence.

    Byte-first splitting must buffer the torn suffix for retry — never decode
    a partial character into replacement text or hand it to the JSON parser.
    """

    def test_chunk_split_inside_a_multibyte_character_round_trips(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        transcript = Path(tempdir.name) / "transcript.jsonl"
        owl = "\U0001f989".encode("utf-8")  # 4 bytes
        record = json.dumps({"note": "owl \U0001f989 intact"}, ensure_ascii=False).encode("utf-8")
        split_at = record.index(owl) + 2  # torn inside the owl

        transcript.write_bytes(b'{"first": true}\n' + record[:split_at])
        lines, partial = cc.read_complete_jsonl_lines(transcript)
        self.assertEqual(lines, ['{"first": true}'])
        self.assertTrue(partial)
        self.assertNotIn("�", "".join(lines))

        with transcript.open("ab") as stream:
            stream.write(record[split_at:] + b"\n")
        lines, partial = cc.read_complete_jsonl_lines(transcript)
        self.assertFalse(partial)
        self.assertEqual(json.loads(lines[1])["note"], "owl \U0001f989 intact")
        self.assertNotIn("�", lines[1])


if __name__ == "__main__":
    unittest.main()
