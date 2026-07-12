"""`team up`, `team grunt add`, `team grunt rm`, and `down`'s pane killing.

No tmux: `panes.Panes` is injected as a fake, exactly as `cli.cmd_*` allows.
What is tested here is the ORDER of operations and what is refused, because
both are the safety properties -- a pane launched before its worktree exists is
rooted in the main tree, and a teardown that kills panes before checking for
uncollected work has destroyed the grunts it was about to refuse to remove.
"""
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from team import api, bus, cli, config, ops, panes, worktrees
from team.config import StateError


class FakePanes:
    def __init__(self, ready=True):
        self.calls: list[tuple] = []
        self.killed: list[str] = []
        self._n = 16
        self._ready = ready

    def split(self, target, cwd, command, env=None):
        self.calls.append(("split", target, str(cwd)))
        self._n += 1
        return f"%{self._n}"

    def pipe_pane(self, target, logfile):
        self.calls.append(("pipe_pane", target))

    def install_death_hook(self, target, script):
        self.calls.append(("install_death_hook", target))

    def wait_ready(self, target, timeout=60.0):
        self.calls.append(("wait_ready", target))
        if not self._ready:
            raise panes.PaneError(f"{target}: no prompt")

    def kill(self, target):
        self.calls.append(("kill", target))
        self.killed.append(target)

    def new_session(self, session, cwd, command):
        self.calls.append(("new_session", session))
        return "%1"

    def exists(self, target):
        return True


class _Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self._git("init", "-q", ".")
        self._git("config", "user.email", "t@t.t")
        self._git("config", "user.name", "t")
        (self.root / "a.txt").write_text("hi\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "init")
        config.init(self.root)
        self.p = FakePanes()
        self.env = mock.patch.dict(os.environ, {"TMUX": "1", "TMUX_PANE": "%1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=str(self.root), check=True,
                       capture_output=True, text=True)

    def _add(self, name=None, command="sh", **kw):
        args = mock.Mock(name_=None)
        args = type("A", (), dict(name=name, window=None, command=command,
                                  timeout=1.0, **kw))()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli.cmd_grunt_add(args, self.root, p=self.p)

    def _rm(self, name, force=False):
        args = type("A", (), dict(name=name, force=force))()
        with redirect_stdout(io.StringIO()):
            return cli.cmd_grunt_rm(args, self.root, p=self.p)

    def _roster(self):
        return bus.read_json(bus.roster_path(self.root))


class GruntAddTest(_Repo):
    def test_the_worktree_exists_before_the_pane_is_split(self):
        """A pane launched before its worktree lands in the main tree, and qwen
        roots its file tools there. Measured, task 013."""
        seen = {}
        real_split = self.p.split

        def spy(target, cwd, command, env=None):
            seen["cwd_existed"] = Path(cwd).is_dir()
            seen["cwd"] = str(cwd)
            return real_split(target, cwd, command, env)

        self.p.split = spy
        self._add()
        self.assertTrue(seen["cwd_existed"])
        self.assertEqual(seen["cwd"], str(worktrees.path(self.root, "grunt1")))

    def test_the_roster_records_the_pane_id_the_split_returned(self):
        self._add()
        self.assertEqual(self._roster()["grunt1"]["pane"], "%17")

    def test_names_are_allocated_lowest_free_first(self):
        self._add()
        self._add()
        self.assertEqual(sorted(self._roster()), ["grunt1", "grunt2"])
        self._rm("grunt1")
        self._add()          # reuses the freed name, on a brand new pane
        self.assertEqual(sorted(self._roster()), ["grunt1", "grunt2"])
        self.assertEqual(self._roster()["grunt1"]["pane"], "%19")
        self.assertEqual(self._roster()["grunt2"]["pane"], "%18")

    def test_an_explicit_name_is_honoured(self):
        self._add(name="scout")
        self.assertIn("scout", self._roster())

    def test_a_duplicate_name_is_refused(self):
        self._add(name="scout")
        with self.assertRaisesRegex(StateError, "already in the roster"):
            self._add(name="scout")

    def test_lead_is_not_a_grunt_name(self):
        with self.assertRaisesRegex(StateError, "not a grunt name"):
            self._add(name="lead")

    def test_refuses_outside_tmux_with_no_window(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(StateError, "not inside tmux"):
                self._add()

    def test_refuses_a_command_not_on_path(self):
        with self.assertRaisesRegex(StateError, "not on PATH"):
            self._add(command="definitely-not-a-real-binary-xyz")

    def test_provisions_settings_into_the_worktree(self):
        self._add()
        self.assertTrue((worktrees.path(self.root, "grunt1") /
                         ".qwen" / "settings.json").is_file())

    def test_the_grunt_is_registered_before_the_readiness_wait(self):
        """A grunt whose TUI never draws still owns a pane. If the roster entry
        came after the wait, `grunt rm` could not find it to clean it up."""
        self.p._ready = False
        with self.assertRaises(panes.PaneError):
            self._add()
        self.assertEqual(self._roster()["grunt1"]["pane"], "%17")

    def test_an_unborn_head_warns_and_falls_back_to_the_main_tree(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(empty, ignore_errors=True))
        subprocess.run(["git", "init", "-q", str(empty)], check=True)
        config.init(empty)
        args = type("A", (), dict(name=None, window="%1", command="sh", timeout=1.0))()
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            cli.cmd_grunt_add(args, empty, p=self.p)
        self.assertIn("no worktree", err.getvalue())
        roster = bus.read_json(bus.roster_path(empty))
        self.assertEqual(roster["grunt1"]["cwd"], str(empty))


class GruntRmTest(_Repo):
    def test_refuses_a_dirty_worktree(self):
        self._add()
        (worktrees.path(self.root, "grunt1") / "work.cs").write_text("x\n")
        with self.assertRaisesRegex(StateError, "uncollected"):
            self._rm("grunt1")
        self.assertIn("grunt1", self._roster())
        self.assertEqual(self.p.killed, [])

    def test_force_discards_it(self):
        self._add()
        (worktrees.path(self.root, "grunt1") / "work.cs").write_text("x\n")
        self._rm("grunt1", force=True)
        self.assertNotIn("grunt1", self._roster())
        self.assertEqual(self.p.killed, ["%17"])
        self.assertFalse(worktrees.path(self.root, "grunt1").exists())

    def test_an_open_task_is_marked_dead(self):
        """Otherwise a re-added grunt of the same name is refused its first
        dispatch, and `wait --task` on the orphan never returns."""
        self._add()
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        self._rm("grunt1")
        self.assertTrue(bus.is_dead(self.root, tid))

    def test_removing_an_unknown_grunt_is_refused(self):
        with self.assertRaises(StateError):
            self._rm("nobody")

    def test_the_lead_cannot_be_removed(self):
        cli._write_roster(self.root, {"lead": {"pane": "%1"}})
        with self.assertRaises(StateError):
            self._rm("lead")


class UpTest(_Repo):
    def _up(self, grunts=0, **kw):
        args = type("A", (), dict(grunts=grunts, session="s", lead_pane=None,
                                  force=False, timeout=1.0, command="sh",
                                  lead_command="sh", **kw))()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli.cmd_up(args, self.root, p=self.p)

    def test_inside_tmux_the_lead_is_the_current_pane_and_no_session_is_made(self):
        self._up()
        self.assertEqual(self._roster()["lead"]["pane"], "%1")
        self.assertNotIn("new_session", [c[0] for c in self.p.calls])

    def test_outside_tmux_a_session_is_created(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self._up()
        self.assertIn("new_session", [c[0] for c in self.p.calls])

    def test_tmux_set_but_pane_unknown_is_refused_not_guessed(self):
        with mock.patch.dict(os.environ, {"TMUX": "1"}, clear=True):
            with self.assertRaisesRegex(StateError, "TMUX_PANE"):
                self._up()

    def test_grunts_default_to_none(self):
        self._up()
        self.assertEqual(sorted(self._roster()), ["lead"])

    def test_n_grunts_are_added(self):
        self._up(grunts=2)
        self.assertEqual(sorted(self._roster()), ["grunt1", "grunt2", "lead"])

    def test_a_live_roster_is_not_silently_overwritten(self):
        self._up()
        self._add()
        with self.assertRaisesRegex(StateError, "orphan"):
            self._up()
        self.assertIn("grunt1", self._roster())


class DownKillsPanesTest(_Repo):
    def _down(self, force=False):
        args = type("A", (), dict(force=force))()
        with redirect_stdout(io.StringIO()):
            return cli.cmd_down(args, self.root, p=self.p)

    def test_grunt_panes_are_killed_and_the_lead_pane_is_not(self):
        """`down` deletes the worktrees. A grunt left running has its cwd pulled
        out from under it. The lead's pane is where the person typing `down` is
        sitting."""
        cli._write_roster(self.root, {"lead": {"pane": "%1"}})
        self._add()
        self._down()
        self.assertEqual(self.p.killed, ["%17"])
        self.assertFalse((self.root / ".team").exists())

    def test_a_refused_down_has_not_killed_anything(self):
        """The uncollected check must run before the panes die, or a refusal
        has already destroyed the grunts it refused to remove."""
        cli._write_roster(self.root, {"lead": {"pane": "%1"}})
        self._add()
        (worktrees.path(self.root, "grunt1") / "work.cs").write_text("x\n")
        with self.assertRaisesRegex(StateError, "uncollected"):
            self._down()
        self.assertEqual(self.p.killed, [])
        self.assertTrue((self.root / ".team").exists())


if __name__ == "__main__":
    unittest.main()


class FailurePathTest(_Repo):
    def test_a_pane_that_cannot_be_piped_is_killed_not_orphaned(self):
        """The pane is not in the roster yet, so nothing else would ever find
        it: an agent left running in a worktree, invisible to `grunt rm` and to
        `down`."""
        self.p.pipe_pane = mock.Mock(side_effect=panes.PaneError("no such pane"))
        with self.assertRaises(panes.PaneError):
            self._add()
        self.assertEqual(self.p.killed, ["%17"])
        self.assertNotIn("grunt1", self._roster())

    def test_a_failed_up_leaves_no_roster_to_force_past(self):
        self.p.pipe_pane = mock.Mock(side_effect=panes.PaneError("no such pane"))
        args = type("A", (), dict(grunts=0, session="s", lead_pane=None,
                                  force=False, timeout=1.0, command="sh",
                                  lead_command="sh"))()
        with redirect_stdout(io.StringIO()), self.assertRaises(panes.PaneError):
            cli.cmd_up(args, self.root, p=self.p)
        self.assertEqual(self._roster(), {})


class TaskPathTest(_Repo):
    """The grunt's cwd is its worktree; the bus lives once, in the main tree.
    A path relative to the main root names nothing from where the grunt stands.
    Measured live: handed `.team/inbox/grunt1/001.json`, the grunt could not
    open it and the task died silently."""

    def test_the_task_path_sent_to_a_grunt_is_absolute(self):
        self._add()
        self.p.clear_context = lambda t: self.p.calls.append(("clear_context", t))
        sent = []
        self.p.send_line = lambda t, text: sent.append(text)
        # The real parser, not a hand-rolled namespace: a fixture that invents
        # `args` drifts silently the moment `send` grows a flag.
        args = cli.build_parser().parse_args(["send", "grunt1", "--question", "q"])
        with redirect_stdout(io.StringIO()):
            cli.cmd_send(args, self.root, p=self.p)
        self.assertEqual(len(sent), 1)
        path = Path(sent[0].removeprefix("do task "))
        self.assertTrue(path.is_absolute(), sent[0])
        self.assertTrue(path.is_file(), path)

    def test_the_task_path_is_readable_from_inside_the_worktree(self):
        self._add()
        sent = []
        self.p.clear_context = lambda t: None
        self.p.send_line = lambda t, text: sent.append(text)
        # The real parser, not a hand-rolled namespace: a fixture that invents
        # `args` drifts silently the moment `send` grows a flag.
        args = cli.build_parser().parse_args(["send", "grunt1", "--question", "q"])
        with redirect_stdout(io.StringIO()):
            cli.cmd_send(args, self.root, p=self.p)
        rel = sent[0].removeprefix("do task ")
        work = worktrees.path(self.root, "grunt1")
        self.assertTrue((work / rel).is_file() or Path(rel).is_file())
        # the failing form: relative to the main root, resolved from the worktree
        self.assertFalse((work / ".team" / "inbox" / "grunt1" / "001.json").exists())


class BootstrapTest(unittest.TestCase):
    """`team bootstrap` turns an empty directory into a dispatching lead."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.p = FakePanes()
        self.env = mock.patch.dict(os.environ, {"TMUX": "1", "TMUX_PANE": "%1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        # Hermetic: bootstrap goes through the cli consent gate, which reads the
        # real ~/.qwen via Path.home() (survives an environ clear -- pwd fallback).
        # Point it at an empty home so there is no provider to consent about.
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        hp = mock.patch.object(Path, "home", return_value=Path(self._home.name))
        hp.start()
        self.addCleanup(hp.stop)

    def _boot(self, root=None, grunts=0, force=False, here=False):
        args = type("A", (), dict(grunts=grunts, session="s", lead_pane=None,
                                  force=force, timeout=1.0, command="sh",
                                  lead_command="sh", here=here,
                                  copy_provider=False, skip_copy=False))()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli.cmd_bootstrap(args, root or self.root, p=self.p)

    def test_an_empty_directory_becomes_a_repo_with_a_bus_and_a_lead(self):
        self._boot()
        self.assertTrue((self.root / ".git").is_dir())
        self.assertTrue((self.root / ".team").is_dir())
        self.assertTrue(worktrees.Worktrees().has_commit(self.root))
        self.assertEqual(sorted(bus.read_json(bus.roster_path(self.root))), ["lead"])

    def test_inside_another_repo_it_auto_applies_here_and_says_so(self):
        """`bus_root()` walks up, so a bus at a nested cwd would resolve to the
        parent and scatter worktrees there. The rule is absolute -- the bus lives
        WHERE YOU STARTED -- so bootstrap git-inits the nested dir as its own repo
        (that is `--here`) rather than refuse-and-wait, and TELLS the user it did,
        naming the `cd {top}` alternative."""
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        sub = self.root / "nested"
        sub.mkdir()
        args = type("A", (), dict(grunts=0, session="s", lead_pane=None,
                                  force=False, timeout=1.0, command="sh",
                                  lead_command="sh", here=False))()
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = cli.cmd_bootstrap(args, sub, p=self.p)
        self.assertEqual(rc, cli.OK)
        self.assertTrue((sub / ".git").is_dir())            # its own repo now
        self.assertTrue((sub / ".team").is_dir())           # bus lives here
        self.assertFalse((self.root / ".team").exists())    # parent left alone
        msg = err.getvalue()
        self.assertIn("NOTE:", msg)                         # it told the user
        self.assertIn("cd", msg)                            # named the alternative

    def test_here_bootstraps_a_nested_dir_as_its_own_project(self):
        """--here: the invocation dir IS the project. It git-inits nested, so the
        bus and every verb resolve here and never climb to the parent repo."""
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        sub = self.root / "nested"
        sub.mkdir()
        rc = self._boot(root=sub, here=True)
        self.assertEqual(rc, cli.OK)
        self.assertTrue((sub / ".git").is_dir())          # its own repo now
        self.assertTrue((sub / ".team").is_dir())          # bus lives here
        self.assertFalse((self.root / ".team").exists())   # parent left alone

    def test_it_is_idempotent(self):
        self._boot()
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                              capture_output=True, text=True).stdout
        self._boot(force=True)          # a live roster needs --force
        again = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                               capture_output=True, text=True).stdout
        self.assertEqual(head, again, "bootstrap made a second commit")

    def test_an_existing_repo_with_commits_gains_no_extra_commit(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        for k, v in (("user.email", "t@t.t"), ("user.name", "t")):
            subprocess.run(["git", "config", k, v], cwd=self.root, check=True)
        (self.root / "f.txt").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True)
        before = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=self.root,
                                capture_output=True, text=True).stdout.strip()
        self._boot()
        after = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=self.root,
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(before, after)

    def test_it_adds_grunts_when_asked(self):
        self._boot(grunts=1)
        self.assertEqual(sorted(bus.read_json(bus.roster_path(self.root))),
                         ["grunt1", "lead"])


class OwnBusGuard(unittest.TestCase):
    """A lead may only operate the bus its OWN pane bootstrapped. Every project's
    bus is `.team`, so a lead whose cwd/env drifted to another project resolves
    that project's bus -- and would dispatch into it. Measured: a task for
    ~/teamTest was written to ~/Projects/IFZ-Modding/.team and run by its grunt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        config.init(self.root)
        bus.write_json(bus.roster_path(self.root),
                       {"lead": {"pane": "%1", "backend": "claude",
                                 "cwd": str(self.root)}})

    def test_refuses_a_pane_that_is_not_this_bus_lead(self):
        with self.assertRaisesRegex(StateError, "cross-contaminate"):
            api.assert_own_bus(self.root, pane="%99")

    def test_allows_the_lead_pane(self):
        api.assert_own_bus(self.root, pane="%1")            # no raise

    def test_dormant_outside_tmux(self):
        api.assert_own_bus(self.root, pane=None)            # no pane -> no check

    def test_dormant_when_no_roster(self):
        bus.roster_path(self.root).unlink()
        api.assert_own_bus(self.root, pane="%99")           # nothing to compare

    def test_send_refuses_from_a_foreign_pane(self):
        with mock.patch.dict(os.environ, {"TMUX_PANE": "%99"}):
            with self.assertRaisesRegex(StateError, "cross-contaminate"):
                api.send(self.root, "grunt1", question="q", scope=["src"])


class PinRepoHere(unittest.TestCase):
    """`_pin_repo_here` is the shared rule behind `bootstrap`: the bus lives WHERE
    YOU START IT, never up the tree. The whole of $HOME is a git repo, so setup in
    ~/teamTest used to resolve to $HOME and create ~/.team plus rewrite the global
    ~/.qwen. It now pins the cwd as its own repo -- creating one, or nesting one
    inside a bigger enclosing repo -- so the bus can never climb out of it. The
    end-to-end pin-here + tell behaviour is covered by BootstrapTest; this pins the
    per-case wording the CLI depends on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_a_bare_directory_is_git_inited_with_no_notice(self):
        actions, notice = cli._pin_repo_here(self.root, worktrees.Worktrees())
        self.assertTrue((self.root / ".git").is_dir())     # made the repo
        self.assertEqual(notice, "")                       # nothing done behind them

    def test_nested_in_a_bigger_repo_it_pins_here_and_names_the_alternative(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        sub = self.root / "scratch"
        sub.mkdir()
        _actions, notice = cli._pin_repo_here(sub, worktrees.Worktrees())
        self.assertTrue((sub / ".git").is_dir())           # its own repo, nested
        self.assertIn("NOTE:", notice)                     # it told the user
        self.assertIn("cd", notice)                        # named the alternative
        self.assertIn(str(self.root), notice)              # the enclosing repo

    def test_at_a_repo_root_there_is_nothing_to_pin(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        for k, v in (("user.email", "t@t.t"), ("user.name", "t")):
            subprocess.run(["git", "config", k, v], cwd=self.root, check=True)
        _actions, notice = cli._pin_repo_here(self.root, worktrees.Worktrees())
        self.assertEqual(notice, "")

    def test_the_home_case_warns_about_the_global_qwen(self):
        """Nested under $HOME the notice must name the specific danger: a bus at
        $HOME rewrites the global ~/.qwen."""
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        sub = self.root / "teamTest"
        sub.mkdir()
        with mock.patch.object(Path, "home", return_value=self.root):
            _actions, notice = cli._pin_repo_here(sub, worktrees.Worktrees())
        self.assertIn("HOME directory", notice)
        self.assertIn("~/.qwen", notice)
