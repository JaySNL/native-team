"""The MCP wrapper: one core, two renderings.

The tests that matter are not the happy paths. They are: does a failing
verification look like a *failure* or like an *error*, and does one bad frame
kill the session.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from team import api, bus, config, mcp_server, ops


def rpc(mid, method, **params):
    return json.dumps({"jsonrpc": "2.0", "id": mid, "method": method,
                       "params": params})


class _Bus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        config.init(self.root)
        (self.root / "src").mkdir()
        (self.root / "src" / "A.cs").write_text("one\ntwo\nthree\n")
        self.cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)

    def _sealed(self, line, evidence, symbol="two"):
        tid = ops.compose_task(self.root, "grunt1", "q", ["src"])
        ops.result_add(self.root, tid, {"file": "src/A.cs", "line": line,
                                        "symbol": symbol, "evidence": evidence})
        ops.result_done(self.root, tid, "grunt1")
        return tid

    def _drive(self, *lines):
        out = io.StringIO()
        mcp_server.serve(io.StringIO("\n".join(lines) + "\n"), out)
        return [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]


class TheHandshake(_Bus):
    def test_initialize_echoes_the_clients_protocol_version(self):
        """Hardcoding a version makes the server lie to a newer client."""
        r, = self._drive(rpc(1, "initialize", protocolVersion="2099-01-01"))
        self.assertEqual(r["result"]["protocolVersion"], "2099-01-01")
        self.assertEqual(r["result"]["serverInfo"]["name"], "team")
        self.assertIn("tools", r["result"]["capabilities"])

    def test_initialize_without_a_version_falls_back(self):
        r, = self._drive(rpc(1, "initialize"))
        self.assertEqual(r["result"]["protocolVersion"],
                         mcp_server.FALLBACK_PROTOCOL)

    def test_ping(self):
        r, = self._drive(rpc(1, "ping"))
        self.assertEqual(r["result"], {})

    def test_tools_list_is_exactly_the_three_lead_verbs(self):
        r, = self._drive(rpc(1, "tools/list"))
        self.assertEqual([t["name"] for t in r["result"]["tools"]],
                         ["team_send", "team_wait", "team_verify"])

    def test_every_tool_declares_a_schema_with_required_args(self):
        r, = self._drive(rpc(1, "tools/list"))
        for tool in r["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object", tool["name"])
            self.assertTrue(tool["inputSchema"]["required"], tool["name"])

    def test_lenient_is_not_offered(self):
        """A tool returning ok=false needs no escape hatch, and offering one
        would be offering the lead a way to launder a fabricated citation."""
        r, = self._drive(rpc(1, "tools/list"))
        blob = json.dumps(r["result"]["tools"])
        self.assertNotIn("lenient", blob)


class TheFramingIsRobust(_Bus):
    def test_a_notification_gets_no_response(self):
        out = self._drive(json.dumps({"jsonrpc": "2.0",
                                      "method": "notifications/initialized"}))
        self.assertEqual(out, [])

    def test_a_garbage_frame_does_not_kill_the_session(self):
        out = self._drive("not json", rpc(2, "ping"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 2)

    def test_a_non_object_frame_is_skipped(self):
        out = self._drive("[1,2,3]", "null", rpc(2, "ping"))
        self.assertEqual([r["id"] for r in out], [2])

    def test_blank_lines_are_skipped(self):
        out = self._drive("", "   ", rpc(2, "ping"))
        self.assertEqual(len(out), 1)

    def test_an_unknown_method_with_an_id_is_a_protocol_error(self):
        r, = self._drive(rpc(1, "resources/list"))
        self.assertEqual(r["error"]["code"], -32601)

    def test_one_response_per_line(self):
        out = io.StringIO()
        mcp_server.serve(io.StringIO(rpc(1, "ping") + "\n" + rpc(2, "ping") + "\n"),
                         out)
        self.assertEqual(len(out.getvalue().strip().splitlines()), 2)


class VerifyReportsRatherThanErrors(_Bus):
    """The load-bearing distinction. A failed verification is a SUCCESSFUL call
    that answers 'no'. isError means the tool could not answer at all."""

    def test_a_passing_citation_is_ok_true(self):
        tid = self._sealed(2, "two")
        r, = self._drive(rpc(1, "tools/call", name="team_verify",
                             arguments={"task": tid}))
        res = r["result"]
        self.assertNotIn("isError", res)
        self.assertTrue(res["structuredContent"]["ok"])
        self.assertEqual(res["structuredContent"]["citations"][0]["status"], "PASS")

    def test_a_wrong_line_number_is_ok_false_and_not_an_error(self):
        tid = self._sealed(3, "two")
        r, = self._drive(rpc(1, "tools/call", name="team_verify",
                             arguments={"task": tid}))
        res = r["result"]
        self.assertNotIn("isError", res)          # it answered; it did not fail
        self.assertFalse(res["structuredContent"]["ok"])
        self.assertEqual(res["structuredContent"]["citations"][0]["status"],
                         "OFF_BY")

    def test_a_failing_verification_leads_with_the_banner(self):
        """Buried under a table, a FAIL reads as a footnote."""
        tid = self._sealed(3, "two")
        r, = self._drive(rpc(1, "tools/call", name="team_verify",
                             arguments={"task": tid}))
        text = r["result"]["content"][0]["text"]
        self.assertTrue(text.startswith(mcp_server.FAIL_BANNER), text[:80])

    def test_a_passing_verification_has_no_banner(self):
        tid = self._sealed(2, "two")
        r, = self._drive(rpc(1, "tools/call", name="team_verify",
                             arguments={"task": tid}))
        self.assertNotIn(mcp_server.FAIL_BANNER,
                         r["result"]["content"][0]["text"])

    def test_the_citation_carries_the_evidence_and_the_detail(self):
        tid = self._sealed(3, "two")
        r, = self._drive(rpc(1, "tools/call", name="team_verify",
                             arguments={"task": tid}))
        c = r["result"]["structuredContent"]["citations"][0]
        self.assertEqual(c["file"], "src/A.cs")
        self.assertEqual(c["line"], 3)
        self.assertEqual(c["evidence"], "two")
        self.assertTrue(c["detail"])

    def test_a_fabricated_citation_is_ok_false(self):
        # `symbol` must appear in `evidence` -- the schema refuses the record
        # otherwise -- so a fabrication quotes a line that is nowhere in the file.
        tid = self._sealed(2, "nowhere in this file", symbol="nowhere")
        r, = self._drive(rpc(1, "tools/call", name="team_verify",
                             arguments={"task": tid}))
        sc = r["result"]["structuredContent"]
        self.assertFalse(sc["ok"])
        self.assertEqual(sc["citations"][0]["status"], "FABRICATED")


class RefusalsAreErrors(_Bus):
    def test_an_unknown_tool_is_an_error(self):
        r, = self._drive(rpc(1, "tools/call", name="team_nope", arguments={}))
        self.assertTrue(r["result"]["isError"])
        self.assertIn("unknown tool", r["result"]["content"][0]["text"])

    def test_verifying_a_task_that_does_not_exist_is_an_error(self):
        r, = self._drive(rpc(1, "tools/call", name="team_verify",
                             arguments={"task": "999"}))
        self.assertTrue(r["result"]["isError"])

    def test_a_missing_required_argument_is_an_error_not_a_crash(self):
        r, = self._drive(rpc(1, "tools/call", name="team_verify", arguments={}))
        self.assertTrue(r["result"]["isError"])

    def test_sending_to_an_agent_with_no_pane_is_an_error(self):
        r, = self._drive(rpc(1, "tools/call", name="team_send",
                             arguments={"agent": "ghost", "question": "q"}))
        self.assertTrue(r["result"]["isError"])
        self.assertIn("refused", r["result"]["content"][0]["text"])

    def test_no_bus_is_an_error_not_a_traceback(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, True))
        os.chdir(outside)
        r, = self._drive(rpc(1, "tools/call", name="team_verify",
                             arguments={"task": "001"}))
        self.assertTrue(r["result"]["isError"])

    def test_the_root_is_resolved_per_call_not_cached(self):
        """A server started before `team bootstrap` would otherwise answer for
        the wrong directory for the rest of the session."""
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, True))
        os.chdir(outside)
        r, = self._drive(rpc(1, "tools/call", name="team_verify",
                             arguments={"task": "001"}))
        self.assertTrue(r["result"]["isError"])
        tid = None
        os.chdir(self.root)
        tid = self._sealed(2, "two")
        r, = self._drive(rpc(1, "tools/call", name="team_verify",
                             arguments={"task": tid}))
        self.assertTrue(r["result"]["structuredContent"]["ok"])


class WaitIsStructured(_Bus):
    def test_a_sealed_task_reports_sealed_and_ok(self):
        tid = self._sealed(2, "two")
        r, = self._drive(rpc(1, "tools/call", name="team_wait",
                             arguments={"tasks": [tid], "timeout": 5}))
        sc = r["result"]["structuredContent"]
        self.assertEqual(sc["sealed"], [tid])
        self.assertTrue(sc["ok"])

    def test_a_timeout_is_ok_false_but_not_an_error(self):
        """`team wait ...; echo done` destroys $?. A tool call has no $?."""
        tid = ops.compose_task(self.root, "grunt1", "q", ["src"])
        r, = self._drive(rpc(1, "tools/call", name="team_wait",
                             arguments={"tasks": [tid], "timeout": 0.1}))
        res = r["result"]
        self.assertNotIn("isError", res)
        self.assertFalse(res["structuredContent"]["ok"])
        self.assertEqual(res["structuredContent"]["timed_out"], [tid])

    def test_a_superseded_task_is_resolved_not_lost(self):
        tid = ops.compose_task(self.root, "grunt1", "q", ["src"])
        bus.mark_dead(self.root, tid)
        r, = self._drive(rpc(1, "tools/call", name="team_wait",
                             arguments={"tasks": [tid], "timeout": 5}))
        sc = r["result"]["structuredContent"]
        self.assertEqual(sc["superseded"], [tid])
        self.assertTrue(sc["ok"])


class TheServerRunsAsAProcess(_Bus):
    def test_a_real_stdio_session_handshakes_and_lists_tools(self):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        proc = subprocess.run(
            [sys.executable, "-m", "team.mcp_server"],
            input=rpc(1, "initialize") + "\n" + rpc(2, "tools/list") + "\n",
            capture_output=True, text=True, cwd=str(self.root), env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        frames = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
        self.assertEqual([f["id"] for f in frames], [1, 2])
        self.assertEqual(len(frames[1]["result"]["tools"]), 3)

    def test_nothing_but_frames_reaches_stdout(self):
        """A stray print is a corrupt frame and a dead session."""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        tid = self._sealed(3, "two")     # a FAILING verify: the noisiest path
        proc = subprocess.run(
            [sys.executable, "-m", "team.mcp_server"],
            input=rpc(1, "tools/call", name="team_verify",
                     arguments={"task": tid}) + "\n",
            capture_output=True, text=True, cwd=str(self.root), env=env, timeout=30)
        for line in proc.stdout.splitlines():
            json.loads(line)             # every line is a frame, or this raises


class ApiIsTheOneCore(_Bus):
    def test_the_server_does_not_reimplement_verify(self):
        """Both surfaces must answer 'is this citation real?' identically."""
        tid = self._sealed(3, "two")
        result = api.verify_task(self.root, tid)
        r, = self._drive(rpc(1, "tools/call", name="team_verify",
                             arguments={"task": tid}))
        self.assertEqual(r["result"]["structuredContent"]["ok"], result.ok)
        self.assertEqual(r["result"]["structuredContent"]["citations"][0]["status"],
                         result.verdicts[0].status)
