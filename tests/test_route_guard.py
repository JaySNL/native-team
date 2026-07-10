"""The guard that makes "do not do the work yourself" more than a sentence.

`decide()` is pure enough to test without Claude: it reads the bus and the
payload, and returns a decision. The one thing it must never do is raise.
"""
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from team import bus, config, ops

_spec = importlib.util.spec_from_file_location(
    "team_route_guard",
    Path(__file__).resolve().parent.parent / "hooks" / "team_route_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

OFF = {"TEAM_ROUTE_GUARD": "0"}
ON: dict = {}


class _Bus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        config.init(self.root)
        (self.root / "src").mkdir()
        (self.root / "src" / "A.cs").write_text("x\n")
        (self.root / "other").mkdir()
        (self.root / "other" / "B.cs").write_text("y\n")

    def _task(self, scope=("src",), agent="grunt1"):
        return ops.compose_task(self.root, agent, "q", list(scope))

    def _decide(self, tool, inp, env=ON, cwd=None):
        return guard.decide(
            {"tool_name": tool, "tool_input": inp, "cwd": str(cwd or self.root)},
            env=dict(env))


class QuietWhenNothingIsInFlight(_Bus):
    def test_no_bus_anywhere_allows(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, True))
        d, _, _ = self._decide("Read", {"file_path": "src/A.cs"}, cwd=outside)
        self.assertEqual(d, "allow")

    def test_a_bus_with_no_open_task_allows(self):
        d, _, _ = self._decide("Read", {"file_path": "src/A.cs"})
        self.assertEqual(d, "allow")

    def test_a_sealed_task_lifts_the_guard(self):
        tid = self._task()
        ops.result_add(self.root, tid, {"file": "src/A.cs", "line": 1,
                                        "symbol": "x", "evidence": "x"})
        ops.result_done(self.root, tid, "grunt1")
        d, _, _ = self._decide("Read", {"file_path": "src/A.cs"})
        self.assertEqual(d, "allow")

    def test_a_superseded_task_lifts_the_guard(self):
        tid = self._task()
        bus.mark_dead(self.root, tid)
        d, _, _ = self._decide("Read", {"file_path": "src/A.cs"})
        self.assertEqual(d, "allow")

    def test_a_build_task_guards_nothing(self):
        self._task(scope=[])
        d, _, _ = self._decide("Read", {"file_path": "src/A.cs"})
        self.assertEqual(d, "allow")

    def test_the_escape_hatch_works(self):
        self._task()
        d, _, _ = self._decide("Read", {"file_path": "src/A.cs"}, env=OFF)
        self.assertEqual(d, "allow")


class DeniesReachingIntoAScope(_Bus):
    def test_reading_a_file_inside_the_scope_is_denied(self):
        tid = self._task()
        d, reason, _ = self._decide("Read", {"file_path": "src/A.cs"})
        self.assertEqual(d, "deny")
        self.assertIn(tid, reason)
        self.assertIn("grunt1", reason)
        self.assertIn("team verify", reason)

    def test_reading_the_scope_itself_is_denied(self):
        self._task(scope=["src/A.cs"])
        d, _, _ = self._decide("Read", {"file_path": "src/A.cs"})
        self.assertEqual(d, "deny")

    def test_an_absolute_path_into_the_scope_is_denied(self):
        self._task()
        d, _, _ = self._decide("Read", {"file_path": str(self.root / "src" / "A.cs")})
        self.assertEqual(d, "deny")

    def test_reading_outside_the_scope_is_allowed(self):
        self._task()
        d, _, ctx = self._decide("Read", {"file_path": "other/B.cs"})
        self.assertEqual(d, "allow")
        self.assertEqual(ctx, "")

    def test_grep_inside_the_scope_is_denied(self):
        self._task()
        d, _, _ = self._decide("Grep", {"pattern": "hp", "path": "src"})
        self.assertEqual(d, "deny")

    def test_glob_reaching_into_the_scope_is_denied(self):
        self._task()
        d, _, _ = self._decide("Glob", {"pattern": "src/**/*.cs"})
        self.assertEqual(d, "deny")

    def test_glob_elsewhere_is_allowed(self):
        self._task()
        d, _, _ = self._decide("Glob", {"pattern": "other/**/*.cs"})
        self.assertEqual(d, "allow")

    def test_a_scope_escaping_the_repo_guards_nothing(self):
        """A scope is a grunt's reading list, not a filesystem ACL."""
        self._task(scope=["../../etc"])
        d, _, _ = self._decide("Read", {"file_path": "/etc/hosts"})
        self.assertEqual(d, "allow")

    def test_two_agents_two_scopes(self):
        self._task(scope=["src"], agent="grunt1")
        self._task(scope=["other"], agent="grunt2")
        self.assertEqual(self._decide("Read", {"file_path": "src/A.cs"})[0], "deny")
        self.assertEqual(self._decide("Read", {"file_path": "other/B.cs"})[0], "deny")


class BashIsTheObviousBypass(_Bus):
    def test_grep_into_the_scope_via_bash_is_denied(self):
        self._task()
        d, _, _ = self._decide("Bash", {"command": "grep -n hp src/A.cs"})
        self.assertEqual(d, "deny")

    def test_cat_into_the_scope_is_denied(self):
        self._task()
        d, _, _ = self._decide("Bash", {"command": "cat src/A.cs"})
        self.assertEqual(d, "deny")

    def test_team_commands_are_never_denied_by_their_own_scope(self):
        """`team send grunt1 --scope src` names src on its own command line.
        Without the allowlist the guard eats `team verify` -- the one verb that
        resolves the situation it created."""
        tid = self._task()
        for cmd in (f"team verify {tid}",
                    "team send grunt1 --question q --scope src --supersede",
                    f"team wait --task {tid}"):
            d, _, _ = self._decide("Bash", {"command": cmd})
            self.assertEqual(d, "allow", cmd)

    def test_an_absolute_team_binary_is_allowed(self):
        self._task()
        d, _, _ = self._decide("Bash", {"command": "/home/u/bin/team verify 001"})
        self.assertEqual(d, "allow")

    def test_unrelated_bash_is_allowed(self):
        self._task()
        d, _, _ = self._decide("Bash", {"command": "ls /tmp"})
        self.assertEqual(d, "allow")

    def test_an_unparseable_command_allows(self):
        self._task()
        d, _, _ = self._decide("Bash", {"command": "echo 'unterminated"})
        self.assertEqual(d, "allow")


class AmbiguityNudgesRatherThanBlocks(_Bus):
    def test_a_repo_wide_grep_is_allowed_with_a_nudge(self):
        """Denying this would block the lead from grepping its own source while
        any task is open, and a guard that blocks ordinary work gets switched
        off."""
        tid = self._task()
        d, reason, ctx = self._decide("Grep", {"pattern": "hp", "path": "."})
        self.assertEqual(d, "allow")
        self.assertEqual(reason, "")
        self.assertIn(tid, ctx)
        self.assertIn("src", ctx)

    def test_the_nudge_names_every_in_flight_task(self):
        self._task(scope=["src"], agent="grunt1")
        self._task(scope=["other"], agent="grunt2")
        _, _, ctx = self._decide("Grep", {"pattern": "hp", "path": "."})
        self.assertIn("grunt1", ctx)
        self.assertIn("grunt2", ctx)


class StaleTasksLetGo(_Bus):
    """A lead that crashed leaves an open task file forever. Without an age cap
    its scope is denied in every future session in that repo -- a guard whose
    failure mode is "this directory is permanently unreadable"."""

    def _age(self, tid, seconds, agent="grunt1"):
        f = bus.team_dir(self.root) / "inbox" / agent / f"{tid}.json"
        old = f.stat().st_mtime - seconds
        os.utime(f, (old, old))

    def test_a_fresh_task_still_guards(self):
        tid = self._task()
        self._age(tid, guard.STALE_AFTER - 60)
        d, _, _ = self._decide("Read", {"file_path": "src/A.cs"})
        self.assertEqual(d, "deny")

    def test_a_stale_task_stops_guarding(self):
        tid = self._task()
        self._age(tid, guard.STALE_AFTER + 60)
        d, _, _ = self._decide("Read", {"file_path": "src/A.cs"})
        self.assertEqual(d, "allow")

    def test_a_stale_task_does_not_even_nudge(self):
        tid = self._task()
        self._age(tid, guard.STALE_AFTER + 60)
        _, _, ctx = self._decide("Grep", {"pattern": "hp", "path": "."})
        self.assertEqual(ctx, "")

    def test_one_stale_task_does_not_lift_a_fresh_one(self):
        stale = self._task(scope=["src"], agent="grunt1")
        self._task(scope=["other"], agent="grunt2")
        self._age(stale, guard.STALE_AFTER + 60, agent="grunt1")
        self.assertEqual(self._decide("Read", {"file_path": "src/A.cs"})[0], "allow")
        self.assertEqual(self._decide("Read", {"file_path": "other/B.cs"})[0], "deny")

    def test_an_unreadable_mtime_is_stale(self):
        """Let go, never hold."""
        self.assertTrue(guard._stale(self.root / "no" / "such.json", 0.0))

    def test_the_cap_exceeds_the_documented_wait(self):
        """TEAMCHAT tells the lead `team wait --timeout 600`. A cap under that
        would lift the guard mid-turn, on a grunt that is still reading."""
        self.assertGreaterEqual(guard.STALE_AFTER, 6 * 600)


class NeverExitsTwo(_Bus):
    """A PreToolUse hook exiting 2 BLOCKS the tool call (measured). This script
    is installed globally, so an exit 2 from it is a session-wide outage."""

    def _run(self, payload: str, cwd=None):
        script = Path(__file__).resolve().parent.parent / "hooks" / "team_route_guard.py"
        return subprocess.run([sys.executable, str(script)], input=payload,
                              capture_output=True, text=True, cwd=str(cwd or self.root))

    def test_every_hostile_payload_exits_zero(self):
        self._task()
        payloads = [
            "", "not json", "null", "[]", "3",
            '{"tool_name": "Read"}',
            '{"tool_name": "Read", "tool_input": null, "cwd": "/nonexistent"}',
            '{"tool_name": "Read", "tool_input": {"file_path": "\\u0000"}, '
            f'"cwd": "{self.root}"}}',
            '{"tool_name": "Bash", "tool_input": {"command": 42}, '
            f'"cwd": "{self.root}"}}',
        ]
        for payload in payloads:
            proc = self._run(payload)
            self.assertEqual(proc.returncode, 0, f"{payload!r} -> {proc.stderr}")
            self.assertEqual(
                json.loads(proc.stdout)["hookSpecificOutput"]["hookEventName"],
                "PreToolUse", payload)

    def test_an_unreadable_bus_exits_zero(self):
        self._task()
        inbox = bus.team_dir(self.root) / "inbox"
        inbox.chmod(0o000)
        self.addCleanup(inbox.chmod, 0o755)
        proc = self._run(json.dumps({"tool_name": "Read",
                                     "tool_input": {"file_path": "src/A.cs"},
                                     "cwd": str(self.root)}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]
                         ["permissionDecision"], "allow")


class TheInstalledCommand(unittest.TestCase):
    """The settings entry, not the script. `python3 <missing file>` exits 2 and
    blocks every matched tool call, so the entry must be the guarded wrapper.

    `SCRIPT` is `$0`, so the path is an argument and never a word of shell --
    a path holding a quote or a space cannot become code.
    """

    SCRIPT = 'test -r "$0" || exit 0; exec python3 "$0"'

    def command(self, path: str) -> str:
        """Exactly the string that goes into settings.json."""
        return f"sh -c {shlex.quote(self.SCRIPT)} {shlex.quote(path)}"

    def _wrapper_rc(self, path):
        return subprocess.run(["sh", "-c", self.SCRIPT, str(path)],
                              input="", capture_output=True, text=True).returncode

    def test_the_settings_string_parses_to_that_argv(self):
        """The thing tested and the thing installed must be one thing."""
        self.assertEqual(shlex.split(self.command("/a b/c'd.py")),
                         ["sh", "-c", self.SCRIPT, "/a b/c'd.py"])

    def test_a_missing_script_would_exit_two_unwrapped(self):
        proc = subprocess.run([sys.executable, "/nonexistent/gone.py"],
                              input="", capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_the_wrapper_turns_that_into_zero(self):
        self.assertEqual(self._wrapper_rc("/nonexistent/gone.py"), 0)

    def test_the_wrapper_still_runs_a_real_script(self):
        real = Path(__file__).resolve().parent.parent / "hooks" / "team_route_guard.py"
        self.assertEqual(self._wrapper_rc(str(real)), 0)

    def test_the_wrapper_survives_an_unreadable_script(self):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            p = Path(f.name)
        self.addCleanup(p.unlink)
        p.chmod(0o000)
        self.assertEqual(self._wrapper_rc(str(p)), 0)

    def test_a_syntax_error_is_non_blocking_not_exit_two(self):
        """Mid-edit breakage in the repo must not block anyone's tool call."""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write("def broken(\n")
            p = Path(f.name)
        self.addCleanup(p.unlink)
        self.assertNotEqual(self._wrapper_rc(str(p)), 2)


class NeverRaises(_Bus):
    def test_a_malformed_payload_allows(self):
        for payload in ({}, {"tool_name": "Read"},
                        {"tool_name": "Read", "tool_input": "not a dict"},
                        {"tool_name": "Read", "tool_input": {}, "cwd": "/nonexistent"}):
            d, _, _ = guard.decide(payload, env={})
            self.assertEqual(d, "allow", payload)

    def test_a_corrupt_task_file_allows(self):
        self._task()
        (bus.team_dir(self.root) / "inbox" / "grunt1" / "001.json").write_text("{oops")
        d, _, _ = self._decide("Read", {"file_path": "src/A.cs"})
        self.assertEqual(d, "allow")

    def test_the_script_emits_valid_json_and_exits_zero(self):
        """The contract is stdout, not the return value."""
        self._task()
        script = Path(__file__).resolve().parent.parent / "hooks" / "team_route_guard.py"
        payload = json.dumps({"tool_name": "Read",
                              "tool_input": {"file_path": "src/A.cs"},
                              "cwd": str(self.root)})
        proc = subprocess.run([sys.executable, str(script)], input=payload,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        hso = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("team verify", hso["permissionDecisionReason"])

    def test_garbage_on_stdin_still_exits_zero_and_allows(self):
        script = Path(__file__).resolve().parent.parent / "hooks" / "team_route_guard.py"
        proc = subprocess.run([sys.executable, str(script)], input="not json",
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        hso = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "allow")


if __name__ == "__main__":
    unittest.main()
