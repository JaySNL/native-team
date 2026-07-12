"""End-to-end milestone (Task 10): drive the full bus against a *scripted*
grunt pane instead of real qwen.

Real qwen is slow and nondeterministic -- explicitly out of scope for an
automated test (see docs/superpowers/sdd/task-10-brief.md's own caveat).
This test instead spawns a plain `sh` pane that polls for its task file and
answers by calling `team result add` / `team result done` itself, so the
whole run is deterministic and re-runnable. It exercises the same tmux
primitives `bin/team-up` uses -- `pipe-pane` and the `pane-died` hook --
without spawning `bin/team-up` itself (which hardcodes real `claude`/`qwen`
commands).

Two tmux quirks were measured live (tmux 3.7b) while building this test and
are load-bearing for how it's written -- see comments at point of use:

  * `pane-died` only fires when `remain-on-exit` is on for that pane;
    with it off (tmux's default) the pane is destroyed immediately and
    `pane-exited` fires instead, never `pane-died`.
  * `tmux kill-pane` unconditionally destroys a pane -- it bypasses
    remain-on-exit and never fires `pane-died` at all. Only the pane's own
    process actually exiting (including being sent a signal) triggers it,
    so "kill the pane" below signals the pane's real PID rather than
    calling `kill-pane`.
"""
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from team import bus, panes, verify

REPO_ROOT = Path(__file__).resolve().parent.parent
CORRECT_EVIDENCE = "    def try_heal(self, c):"
FABRICATED_EVIDENCE = "    def try_heal(self, c, extra):"  # never appears in bed.py


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def _team_argv(root: Path, *args: str) -> list[str]:
    return [sys.executable, "-m", "team", "--root", str(root), *args]


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        # Hermetic HOME: the scripted grunt needs no real provider, and `team init`
        # would otherwise refuse (consent gate sees the dev's real ~/.qwen provider).
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.env = dict(_env(), HOME=self._home.name)

        self._git("init", "-q")
        (self.root / "bed.py").write_text(
            "class Bed:\n"
            "    def try_heal(self, c):\n"
            "        return True\n"
        )
        self._git("add", "-A")
        self._git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")

        self.session = f"team-e2e-{os.getpid()}-{id(self) % 100000}"
        self.addCleanup(self._kill_session)

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)

    def _kill_session(self) -> None:
        subprocess.run(["tmux", "kill-session", "-t", self.session], capture_output=True)

    def _team(self, *args: str, check: bool = True, timeout: float = 30):
        return subprocess.run(_team_argv(self.root, *args), cwd=self.root, env=self.env,
                               capture_output=True, text=True, check=check, timeout=timeout)

    def _pane_pid(self, pane: str) -> int:
        out = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{pane_pid}"],
                              check=True, capture_output=True, text=True)
        return int(out.stdout.strip())

    def _write_grunt_script(self) -> Path:
        """A `sh` script standing in for qwen: polls for its own task file,
        then answers with one correct citation and one fabricated one (a
        symbol that appears in the evidence text, but at a line the file
        does not have), exactly the shape `team verify` must catch.

        Lives under its own mktemp dir, not under $TEAM -- run-shell's
        argument for a *hook* must avoid spaces in its path (see the module
        docstring), and the pane's own start command is not immune to the
        same class of mistake, so the same discipline is used here too.
        """
        scratch = Path(tempfile.mkdtemp(prefix="team-e2e-grunt-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        script = scratch / "grunt1.sh"
        taskfile = bus.task_path(self.root, "grunt1", "001")
        py = shlex.quote(sys.executable)
        root = shlex.quote(str(self.root))
        script.write_text(
            "#!/bin/sh\n"
            "set -e\n"
            # This script stands in for a qwen TUI, so it must also stand in for
            # the prompt `panes.wait_ready` looks for. Without it `team send`
            # correctly refuses to type into a pane that is not listening.
            'echo "  YOLO mode (shift + tab to cycle)"\n'
            f"while [ ! -f {shlex.quote(str(taskfile))} ]; do sleep 0.1; done\n"
            f"export PYTHONPATH={shlex.quote(str(REPO_ROOT))}\n"
            f"{py} -m team --root {root} result add --task 001 --file bed.py --line 2 "
            f"--symbol try_heal --evidence {shlex.quote(CORRECT_EVIDENCE)}\n"
            f"{py} -m team --root {root} result add --task 001 --file bed.py --line 50 "
            f"--symbol try_heal --evidence {shlex.quote(FABRICATED_EVIDENCE)}\n"
            f"{py} -m team --root {root} result done --task 001 --agent grunt1\n"
            # Stay alive so the test can kill the pane's real process later
            # (step 7) instead of racing a script that has already exited.
            "exec sleep 3600\n"
        )
        script.chmod(0o755)
        return script

    def _write_hook_script(self, name: str) -> Path:
        hookdir = Path(tempfile.mkdtemp(prefix="team-e2e-hooks-"))
        self.addCleanup(shutil.rmtree, hookdir, ignore_errors=True)
        script = hookdir / f"{name}-died.sh"
        script.write_text(
            "#!/bin/sh\n"
            f"export PYTHONPATH={shlex.quote(str(REPO_ROOT))}\n"
            f"exec {shlex.quote(sys.executable)} -m team --root {shlex.quote(str(self.root))} "
            f"msg --agent {shlex.quote(name)} --failed --task pane-died "
            f"{shlex.quote('grunt pane died')}\n"
        )
        script.chmod(0o755)
        return script

    def test_full_bus_with_scripted_grunt(self):
        # -- 1. team init --
        init = self._team("init")
        self.assertEqual(init.returncode, 0, init.stderr)

        # -- 2. create the grunt pane; pipe-pane it; set the death hook --
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", self.session, "-c", str(self.root), "sleep 3600"],
            check=True,
        )
        grunt_script = self._write_grunt_script()
        subprocess.run(
            ["tmux", "split-window", "-t", f"{self.session}:0", "-c", str(self.root),
             str(grunt_script)],
            check=True,
        )
        subprocess.run(["tmux", "select-layout", "-t", f"{self.session}:0", "tiled"], check=True)
        grunt_pane = f"{self.session}:0.1"

        logdir = bus.team_dir(self.root) / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        panes.Panes().pipe_pane(grunt_pane, logdir / "grunt1.log")

        # remain-on-exit must be on *before* the hook can ever fire as
        # pane-died -- measured live, see module docstring.
        subprocess.run(["tmux", "set-option", "-p", "-t", grunt_pane, "remain-on-exit", "on"],
                        check=True)
        hookscript = self._write_hook_script("grunt1")
        subprocess.run(
            ["tmux", "set-hook", "-p", "-t", grunt_pane, "pane-died",
             f"run-shell {shlex.quote(str(hookscript))}"],
            check=True,
        )

        bus.write_json(bus.team_dir(self.root) / "roster.json",
                        {"grunt1": {"pane": grunt_pane, "backend": "bash"}})

        # -- 5. team wait --for lead, backgrounded -- started *before* team
        # send, not after. `wait.for_lead` snapshots the lead inbox's current
        # contents as "before" and only ever reports files that show up
        # after that snapshot. compose_task() writes the task file as the
        # very first thing `cmd_send` does, before its ~0.6s of scripted
        # Escape/clear/send-keys pacing sleeps in panes.py -- long enough for
        # this scripted grunt (unlike real qwen) to notice the task, run all
        # three `team result` subprocess calls, and post the announcement
        # before a `team send` subprocess invoked first would even return.
        # Starting `wait` first (with a moment to reach its snapshot) is what
        # a real lead would also do -- background the wait immediately after
        # telling a grunt to go -- and it is what makes the "fresh" check
        # meaningful instead of racing a message that already landed.
        wait_proc = subprocess.Popen(
            _team_argv(self.root, "wait", "--for", "lead", "--timeout", "30"),
            cwd=self.root, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(1.0)

        # -- 3. compose a task with team send --
        send = self._team("send", "grunt1", "--question", "Where is try_heal defined?",
                          "--scope", "bed.py")
        self.assertEqual(send.returncode, 0, send.stderr)
        self.assertIn("sent task 001", send.stdout)

        # the grunt script (step 4) answers concurrently.
        try:
            out, err = wait_proc.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            wait_proc.kill()
            wait_proc.communicate()
            raise
        self.assertEqual(wait_proc.returncode, 0, f"stdout={out!r} stderr={err!r}")

        # The seal-then-announce invariant (team/ops.py:result_done): the
        # sealed result must already be on disk by the time the announcement
        # that woke `wait` exists. Checking existence *after* wait returns is
        # NOT sufficient to prove ordering -- both writes are microseconds
        # apart in the same function call, so by the time this process next
        # runs, a reversed implementation would *also* have finished sealing.
        # Comparing mtimes instead pins the actual write order, because each
        # atomic_write's mtime is set when its data is written (before the
        # rename), not when this test happens to observe it.
        inbox_files = sorted(bus.lead_inbox(self.root).glob("*.json"))
        self.assertEqual(len(inbox_files), 1, inbox_files)
        announce = bus.read_json(inbox_files[0])
        self.assertEqual(announce["type"], "result")
        result_path = bus.result_path(self.root, "001")
        self.assertTrue(
            result_path.exists(),
            "wait woke on the announcement but the sealed result is not on disk",
        )
        self.assertLessEqual(
            result_path.stat().st_mtime_ns,
            inbox_files[0].stat().st_mtime_ns,
            "seal-then-announce violated: the result file must be written no "
            "later than the message that announces it",
        )

        # -- 6. team verify: a grunt's word is never trusted. One citation is
        # correct, one is fabricated (line 50 of a 3-line file); verify must
        # catch it and any_failed must be true. --
        payload = bus.read_json(result_path)
        self.assertEqual(len(payload["records"]), 2)
        verdicts = verify.verify_records(self.root, payload["records"])
        statuses = {v.status for v in verdicts}
        self.assertIn("PASS", statuses, statuses)
        self.assertTrue(statuses & {"OFF_BY", "FABRICATED"}, statuses)
        self.assertTrue(verify.any_failed(verdicts))

        # Fails closed with no flag: `team verify $t && use_result` must not
        # trust the fabricated citation this grunt just staged.
        cli_verify = self._team("verify", "001", check=False)
        self.assertEqual(cli_verify.returncode, 1, cli_verify.stdout)
        lenient = self._team("verify", "001", "--lenient", check=False)
        self.assertEqual(lenient.returncode, 0, lenient.stdout)
        self.assertTrue(
            "OFF_BY" in cli_verify.stdout or "FABRICATED" in cli_verify.stdout,
            cli_verify.stdout,
        )

        # -- 7. kill the pane; a `failed` message must appear via the hook.
        # `tmux kill-pane` bypasses remain-on-exit and never fires
        # pane-died (measured) -- signal the pane's real process instead. --
        before = {p.name for p in bus.lead_inbox(self.root).glob("*.json")}
        os.kill(self._pane_pid(grunt_pane), signal.SIGTERM)

        deadline = time.monotonic() + 10
        fresh: set[str] = set()
        while time.monotonic() < deadline and not fresh:
            fresh = {p.name for p in bus.lead_inbox(self.root).glob("*.json")} - before
            if not fresh:
                time.sleep(0.1)
        self.assertTrue(fresh, "no new lead-inbox message after the grunt pane died")
        died = bus.read_json(bus.lead_inbox(self.root) / next(iter(fresh)))
        self.assertEqual(died["type"], "failed")
        self.assertEqual(died["from"], "grunt1")
        self.assertEqual(died["task"], "pane-died")

        # -- 8. team down: tears down the bus runtime, leaves the project .qwen --
        down = self._team("down")
        self.assertEqual(down.returncode, 0, down.stderr)
        self.assertFalse(bus.team_dir(self.root).exists())
        # the project settings are project-owned and persist across down
        self.assertTrue((self.root / ".qwen" / "settings.json").exists())


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TeamUpRosterTest(unittest.TestCase):
    """`team up` writes a machine-readable roster keyed on tmux PANE IDS.

    Two bugs are pinned here.

    Python 3.14's `json.tool` honours FORCE_COLOR even when stdout is a file,
    so piping roster.json through it wrote ANSI escapes into the JSON and every
    `team send` failed with "unreadable bus file". Reproduced on the first real
    fan-out run.

    And pane *indices* renumber when a pane dies -- kill grunt1 of three and
    grunt2's pane becomes index 1, so `team send grunt1` types into grunt2's
    pane. Pane ids never move. This test kills a grunt and proves the surviving
    grunt's roster entry still names its own pane.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.session = f"team-test-{os.getpid()}"
        self.addCleanup(lambda: subprocess.run(
            ["tmux", "kill-session", "-t", self.session],
            stderr=subprocess.DEVNULL, check=False))
        self.repo = Path(__file__).resolve().parent.parent
        # Hermetic HOME: no real provider needed here, and `team init`/`bootstrap`
        # would otherwise refuse when the dev's real ~/.qwen has a provider.
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.env = dict(os.environ, FORCE_COLOR="3", PYTHONPATH=str(self.repo),
                        HOME=self._home.name)
        self.env.pop("TMUX", None)          # `up` must create its own session
        self.env.pop("TMUX_PANE", None)

    def _agent(self) -> Path:
        """A fake agent: prints the prompt `wait_ready` looks for, then idles."""
        scratch = Path(tempfile.mkdtemp(prefix="team-e2e-agent-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        script = scratch / "agent"
        script.write_text('#!/bin/sh\necho ">   Type your message or @path"\nexec sleep 60\n')
        script.chmod(0o755)
        return script

    def _team(self, *args):
        return subprocess.run([str(self.repo / "bin" / "team"), *args],
                              cwd=self.root, env=self.env, check=True,
                              capture_output=True, text=True)

    def test_roster_records_pane_ids_and_survives_a_dead_neighbour(self):
        agent = self._agent()
        self._team("init")
        self._team("up", "2", "--session", self.session,
                   "--lead-command", str(agent), "--command", str(agent))

        raw = (self.root / ".team" / "roster.json").read_bytes()
        self.assertNotIn(b"\x1b", raw, "roster.json contains ANSI escapes")
        roster = json.loads(raw)
        self.assertEqual(sorted(roster), ["grunt1", "grunt2", "lead"])
        for name, entry in roster.items():
            self.assertRegex(entry["pane"], r"^%\d+$", f"{name} is not a pane id")
        self.assertEqual(len({e["pane"] for e in roster.values()}), 3)

        # grunt1 dies. Its index would be inherited by grunt2; its id cannot be.
        doomed = roster["grunt1"]["pane"]
        survivor = roster["grunt2"]["pane"]
        subprocess.run(["tmux", "kill-pane", "-t", doomed], check=True)

        live = subprocess.run(["tmux", "list-panes", "-t", self.session,
                                "-F", "#{pane_id}"], capture_output=True, text=True)
        ids = live.stdout.split()
        self.assertNotIn(doomed, ids)
        self.assertIn(survivor, ids)

        p = panes.Panes()
        self.assertFalse(p.exists(doomed))
        self.assertTrue(p.exists(survivor))
