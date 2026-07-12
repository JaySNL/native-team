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

    def wait_ready(self, target, timeout=60.0):
        self.calls.append(("wait_ready", target))

    def kill(self, target):
        self.calls.append(("kill", target))

    def split(self, target, cwd, command, env=None):
        self.calls.append(("split", target, str(cwd), command))
        self._next = getattr(self, "_next", 16) + 1
        return f"%{self._next}"

    def pipe_pane(self, target, logfile):
        self.calls.append(("pipe_pane", target))

    def install_death_hook(self, target, script):
        self.calls.append(("install_death_hook", target))

    def new_session(self, session, cwd, command):
        self.calls.append(("new_session", session))
        return "%1"


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()
        (self.root / "a.py").write_text(SRC)
        # Hermetic: the init/bootstrap consent gate reads the real ~/.qwen via
        # Path.home(); point it at an empty home so there is no provider to
        # consent about (else a dev with a configured qwen makes these refuse).
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        hp = mock.patch.object(Path, "home", return_value=Path(self._home.name))
        hp.start()
        self.addCleanup(hp.stop)

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

    def _sealed_task(self):
        self.run_cli("init")
        (bus.team_dir(self.root) / "inbox" / "grunt1").mkdir(parents=True)
        tid = ops.compose_task(self.root, "grunt1", "q", ["a.py"])
        ops.result_add(self.root, tid, {"file": "a.py", "line": 1,
                                        "symbol": "x", "evidence": "x = 1"})
        ops.result_done(self.root, tid, "grunt1")
        return tid

    def _corrupt_the_line(self, tid):
        bad = bus.read_json(bus.result_path(self.root, tid))
        bad["records"][0]["line"] = 2
        bus.write_json(bus.result_path(self.root, tid), bad)

    def test_verify_passes_clean_result_with_exit_zero(self):
        tid = self._sealed_task()
        code, out = self.run_cli("verify", tid)
        self.assertEqual(code, 0)
        self.assertIn("1 PASS", out)

    def test_verify_fails_closed_by_default(self):
        """The whole point of the project. A lead running
        `team verify $t && use_result` must not trust a fabricated citation
        because it forgot a flag. Observed live: qwen cited line 10 for a
        symbol on line 8, and a permissive default exited 0.
        """
        tid = self._sealed_task()
        self._corrupt_the_line(tid)
        code, out = self.run_cli("verify", tid)
        self.assertEqual(code, 1)
        self.assertIn("OFF_BY", out)

    def test_verify_missing_task_is_refused_not_a_traceback(self):
        """Exit 1 here would be indistinguishable from VERIFY_FAIL, and a
        FileNotFoundError traceback is never a user-facing error.
        """
        self.run_cli("init")
        code, out = self.run_cli("verify", "999")
        self.assertEqual(code, 3, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("no such file", out)

    def test_show_missing_message_is_refused_not_a_traceback(self):
        self.run_cli("init")
        code, out = self.run_cli("show", "999")
        self.assertEqual(code, 3, out)
        self.assertNotIn("Traceback", out)

    def test_send_before_init_is_refused_not_a_traceback(self):
        code, out = self.run_cli("send", "grunt1",
                                 "--question", "q", "--scope", "a.py")
        self.assertEqual(code, 3, out)
        self.assertNotIn("Traceback", out)

    def test_inbox_skips_a_corrupt_file_instead_of_crashing(self):
        self.run_cli("init")
        (bus.team_dir(self.root) / "inbox" / "grunt1").mkdir(parents=True)
        tid = ops.compose_task(self.root, "grunt1", "q", ["a.py"])
        ops.post_message(self.root, "grunt1", "note", tid, "a real message")
        (bus.lead_inbox(self.root) / "001.json").write_text("not json")
        code, out = self.run_cli("inbox")
        self.assertEqual(code, 0, out)
        self.assertIn("<unreadable>", out)
        self.assertIn("a real message", out)

    def test_verify_lenient_is_the_deliberate_opt_out(self):
        tid = self._sealed_task()
        self._corrupt_the_line(tid)
        code, out = self.run_cli("verify", tid, "--lenient")
        self.assertEqual(code, 0)
        self.assertIn("OFF_BY", out, "a lenient exit must still print the failure")

    def test_repeated_task_flags_accumulate(self):
        """`--task 001 --task 002` must wait on BOTH. A bare nargs="*" let the
        second flag replace the first, so the lead waited on one task while
        believing it waited on two -- silently. Observed live.
        """
        args = cli.build_parser().parse_args(
            ["wait", "--task", "001", "--task", "002", "--timeout", "5"])
        self.assertEqual(args.task, ["001", "002"])

    def test_wait_reports_a_superseded_task_instead_of_silence(self):
        """A superseded task resolves but never seals. Reporting it as neither
        SEALED nor TIMEOUT made `team wait` print nothing and exit 0.
        """
        self.run_cli("init")
        (bus.team_dir(self.root) / "inbox" / "grunt1").mkdir(parents=True)
        tid = ops.compose_task(self.root, "grunt1", "q", ["a.py"])
        ops.result_add(self.root, tid, {"file": "a.py", "line": 1,
                                        "symbol": "x", "evidence": "x = 1"})
        ops.result_done(self.root, tid, "grunt1")
        dead = ops.compose_task(self.root, "grunt1", "q2", ["a.py"])
        bus.mark_dead(self.root, dead)

        code, out = self.run_cli("wait", "--task", tid, "--task", dead,
                                 "--timeout", "0")
        self.assertEqual(code, 0, out)
        self.assertIn(f"SEALED: {tid}", out)
        self.assertIn(f"SUPERSEDED: {dead}", out)

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
            [c[0] for c in fake.calls],
            ["exists", "wait_ready", "clear_context", "send_line"])

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
        # wait_ready only captures the pane; send_line, whose leading Escape
        # would cancel a working grunt's turn, is never reached.
        self.assertEqual(fake.calls,
                         [("exists", "team:0.1"), ("wait_ready", "team:0.1")])


if __name__ == "__main__":
    unittest.main()


class DefaultAgentTest(unittest.TestCase):
    def test_infers_grunt_name_from_worktree_cwd(self):
        with mock.patch.object(cli.Path, "cwd",
                               return_value=Path("/repo/.team/work/grunt2/sub")):
            self.assertEqual(cli._default_agent(), "grunt2")

    def test_named_bus_worktree_too(self):
        with mock.patch.object(cli.Path, "cwd",
                               return_value=Path("/repo/.team-auth/work/grunt5")):
            self.assertEqual(cli._default_agent(), "grunt5")

    def test_falls_back_to_grunt1_outside_a_worktree(self):
        with mock.patch.object(cli.Path, "cwd", return_value=Path("/repo/src")):
            self.assertEqual(cli._default_agent(), "grunt1")


class RootResolutionTest(unittest.TestCase):
    """`init`/`down` locate the repo by `.git`; every other verb locates the
    bus by `.team`. The distinction exists so a grunt inside a build task's
    worktree (`.team/work/<agent>`, whose `.git` is a gitdir *file*) still
    addresses the one real bus."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / ".git").mkdir()
        self.cwd = mock.patch("pathlib.Path.cwd", return_value=self.root)
        # Hermetic home: the init consent gate reads ~/.qwen via Path.home().
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        hp = mock.patch.object(Path, "home", return_value=Path(self._home.name))
        hp.start()
        self.addCleanup(hp.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv, cwd):
        with mock.patch("pathlib.Path.cwd", return_value=cwd), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            return cli.main(argv), err.getvalue()

    def test_verbs_refuse_before_init_naming_the_missing_bus(self):
        code, err = self._run(["inbox"], self.root)
        self.assertEqual(code, cli.REFUSED)
        self.assertIn(".team/ bus found", err)

    def test_init_works_with_no_bus_present(self):
        code, _ = self._run(["init"], self.root)
        self.assertEqual(code, cli.OK)
        self.assertTrue((self.root / ".team").is_dir())

    def test_a_verb_run_from_a_worktree_finds_the_outer_bus(self):
        self._run(["init"], self.root)
        wt = self.root / ".team" / "work" / "grunt1"
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /elsewhere\n")

        code, err = self._run(["inbox"], wt)
        self.assertEqual(code, cli.OK, err)

    def test_explicit_root_overrides_discovery(self):
        self._run(["init"], self.root)
        with tempfile.TemporaryDirectory() as elsewhere:
            code, err = self._run(
                ["--root", str(self.root), "inbox"], Path(elsewhere))
            self.assertEqual(code, cli.OK, err)


class BriefTest(unittest.TestCase):
    """`team brief` is what a lead runs after a /compact has cost it the path.
    It must not need a bus, a repo, or a cwd it recognises."""

    def _run(self, argv, cwd):
        with mock.patch("pathlib.Path.cwd", return_value=Path(cwd)), \
             redirect_stdout(io.StringIO()) as out, \
             redirect_stderr(io.StringIO()):
            return cli.main(argv), out.getvalue()

    def test_brief_prints_a_path_that_exists(self):
        with tempfile.TemporaryDirectory() as d:   # no .git, no .team
            code, out = self._run(["brief"], d)
        self.assertEqual(code, cli.OK)
        self.assertTrue(Path(out.strip()).is_file())

    def test_brief_show_prints_the_rules_not_the_path(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = self._run(["brief", "--show"], d)
        self.assertEqual(code, cli.OK)
        self.assertIn("A grunt's citation is not a fact", out)

    def test_brief_refuses_rather_than_crashing_when_the_file_is_gone(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(cli, "BRIEF", Path(d) / "absent.md"):
                code, _ = self._run(["brief"], d)
        self.assertEqual(code, cli.REFUSED)

    def test_the_brief_documents_the_real_cli_surface(self):
        """A brief that names a flag the parser does not have is worse than no
        brief. Check the verbs it teaches actually parse."""
        text = cli.BRIEF.read_text()
        for argv in (["send", "grunt1", "--question", "q", "--scope", "a/b"],
                     ["wait", "--task", "007", "--timeout", "600"],
                     ["verify", "007", "--lenient"],
                     ["send", "grunt1", "--reply", "008", "hi"],
                     ["send", "grunt1", "--supersede", "--question", "q"],
                     ["log", "grunt1", "--tail", "5"],
                     ["inbox"], ["show", "008"], ["down", "--force"]):
            with self.subTest(argv=argv):
                cli.build_parser().parse_args(argv)   # must not SystemExit
                self.assertIn(argv[0], text)


class WorktreeErrorTest(unittest.TestCase):
    """A failing git worktree operation is a refusal (exit 3), never a
    traceback. `team up` runs `worktree up` in repos that may have no commits
    and reports its own warning on a non-zero exit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        import subprocess
        subprocess.run(["git", "init", "-q", "."], cwd=str(self.root), check=True,
                       capture_output=True)          # no commits: HEAD is unborn
        config.init(self.root)
        bus.write_json(bus.team_dir(self.root) / "roster.json",
                       {"lead": {"pane": "t:0.0"}, "grunt1": {"pane": "t:0.1"}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_worktree_up_on_an_unborn_head_is_refused_not_crashed(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli.main(["--root", str(self.root), "worktree", "up"])
        self.assertEqual(code, cli.REFUSED)
        self.assertIn("no commits", err.getvalue())
