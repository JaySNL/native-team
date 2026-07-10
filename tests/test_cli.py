import io, tempfile, unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from team import bus, cli, config, ops, panes

SRC = "x = 1\ny = 2\n"


class _FakePane:
    """Records which Panes methods cli.py invokes, in order, without ever
    touching real tmux. `exists` is canned; `clear_context`/`send_line`
    always "succeed" and just log the call.
    """

    def __init__(self, exists=True):
        self.calls: list[tuple] = []
        self._exists = exists

    def exists(self, target):
        self.calls.append(("exists", target))
        return self._exists

    def clear_context(self, target):
        self.calls.append(("clear_context", target))

    def send_line(self, target, text):
        self.calls.append(("send_line", target, text))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()
        (self.root / "a.py").write_text(SRC)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = cli.main(["--root", str(self.root), *args])
        return code, out.getvalue()

    def _set_roster(self, agent, pane):
        path = bus.team_dir(self.root) / "roster.json"
        roster = bus.read_json(path)
        roster[agent] = {"pane": pane}
        bus.write_json(path, roster)

    def test_init_then_stale_init_exits_3(self):
        self.assertEqual(self.run_cli("init")[0], 0)
        self.assertEqual(self.run_cli("init")[0], 3)
        self.assertEqual(self.run_cli("init", "--force")[0], 0)

    def test_verify_pass_and_strict_fail(self):
        self.run_cli("init")
        (bus.team_dir(self.root) / "inbox" / "grunt1").mkdir(parents=True)
        tid = ops.compose_task(self.root, "grunt1", "q", ["a.py"])
        ops.result_add(self.root, tid, {"file": "a.py", "line": 1,
                                        "symbol": "x", "evidence": "x = 1"})
        ops.result_done(self.root, tid, "grunt1")
        code, out = self.run_cli("verify", tid)
        self.assertEqual(code, 0)
        self.assertIn("1 PASS", out)

        bad = bus.read_json(bus.result_path(self.root, tid))
        bad["records"][0]["line"] = 2
        bus.write_json(bus.result_path(self.root, tid), bad)
        code, out = self.run_cli("verify", tid, "--strict")
        self.assertEqual(code, 1)
        self.assertIn("OFF_BY", out)

    def test_result_add_schema_violation_exits_3(self):
        self.run_cli("init")
        (bus.team_dir(self.root) / "inbox" / "grunt1").mkdir(parents=True)
        tid = ops.compose_task(self.root, "grunt1", "q", ["a.py"])
        code, out = self.run_cli("result", "add", "--task", tid, "--file", "a.py",
                                 "--line", "1", "--symbol", "zzz",
                                 "--evidence", "x = 1")
        self.assertEqual(code, 3)
        self.assertIn("does not appear in evidence", out)

    def test_wait_task_timeout_exits_4(self):
        self.run_cli("init")
        code, out = self.run_cli("wait", "--task", "001", "--timeout", "0")
        self.assertEqual(code, 4)
        self.assertIn("TIMEOUT: 001", out)

    def test_log_renders_stripped_transcript(self):
        self.run_cli("init")
        logfile = bus.team_dir(self.root) / "logs" / "grunt1.log"
        logfile.write_text("\x1b[32m◆ HI\x1b[0m\n... (1.0s · esc to cancel)\n")
        code, out = self.run_cli("log", "grunt1")
        self.assertEqual(code, 0)
        self.assertIn("◆ HI", out)
        self.assertNotIn("esc to cancel", out)

    def test_down_is_clean(self):
        self.run_cli("init")
        code, _ = self.run_cli("down")
        self.assertEqual(code, 0)
        self.assertFalse(bus.team_dir(self.root).exists())

    def test_wait_for_lead_parses_and_times_out(self):
        """`--for lead` must take a value, not be a store_const flag: a
        store_const would leave the bare word "lead" as an unconsumed
        positional and argparse would reject the whole command (exit 2 from
        argparse itself) instead of reaching cmd_wait's TIMEOUT (4).
        """
        self.run_cli("init")
        code, out = self.run_cli("wait", "--for", "lead", "--timeout", "0")
        self.assertEqual(code, 4)
        self.assertIn("TIMEOUT: no message for lead within 0.0s", out)

    def test_send_pane_gone_exits_2(self):
        self.run_cli("init")
        self._set_roster("grunt1", "team:0.1")
        fake = _FakePane(exists=False)
        with mock.patch("team.panes.Panes", return_value=fake):
            code, out = self.run_cli("send", "grunt1", "--question", "q")
        self.assertEqual(code, 2)
        self.assertIn("pane team:0.1 for grunt1 is gone", out)
        self.assertEqual(fake.calls, [("exists", "team:0.1")])

    def test_send_supersede_escapes_old_turn_before_resending(self):
        """--supersede must genuinely halt the grunt's in-flight turn, not
        just mark the old task dead in the bus. clear_context's leading
        Escape is what does that (see panes.py), and it must fire before the
        new task is sent.
        """
        self.run_cli("init")
        self._set_roster("grunt1", "team:0.1")
        old_tid = ops.compose_task(self.root, "grunt1", "old q", [])
        fake = _FakePane(exists=True)
        with mock.patch("team.panes.Panes", return_value=fake):
            code, out = self.run_cli(
                "send", "grunt1", "--question", "new q", "--supersede")
        self.assertEqual(code, 0)
        self.assertTrue(bus.is_dead(self.root, old_tid))
        self.assertEqual(
            [c[0] for c in fake.calls], ["exists", "clear_context", "send_line"])

    def test_send_pane_error_mid_delivery_exits_2_not_traceback(self):
        """A PaneError raised after the exists() check (e.g. the pane dies
        between the check and the send) must map to PANE_GONE, not escape
        cli.main as an unhandled exception.
        """
        self.run_cli("init")
        self._set_roster("grunt1", "team:0.1")
        fake = _FakePane(exists=True)
        fake.clear_context = mock.Mock(
            side_effect=panes.PaneError("team:0.1: pane vanished mid-send"))
        with mock.patch("team.panes.Panes", return_value=fake):
            code, out = self.run_cli("send", "grunt1", "--question", "q")
        self.assertEqual(code, 2)
        self.assertIn("pane vanished mid-send", out)

    def test_send_reply_refused_when_last_message_not_blocked(self):
        """--reply is only permitted when the agent's last message was
        `blocked`. Refusing it must surface as exit 3, not a traceback --
        and critically, must never reach send_line, whose leading Escape
        would otherwise cancel a working grunt's in-flight turn.
        """
        self.run_cli("init")
        self._set_roster("grunt1", "team:0.1")
        fake = _FakePane(exists=True)
        with mock.patch("team.panes.Panes", return_value=fake):
            code, out = self.run_cli(
                "send", "grunt1", "--reply", "001", "answer text")
        self.assertEqual(code, 3)
        self.assertIn("refused:", out)
        self.assertEqual(fake.calls, [("exists", "team:0.1")])


if __name__ == "__main__":
    unittest.main()
