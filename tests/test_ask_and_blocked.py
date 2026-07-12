"""Ask tasks (prose, no citations) and the blocked-wait path.

The demo that motivated these: a grunt sent an ELI5 as a `find` task answered
it perfectly, could not seal (no citations), posted `--blocked`, and its lead
slept the full timeout with the answer already in its inbox. Three defects, one
per class below.
"""
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from team import api, bus, config, ops, wait
from team.config import StateError


class _Bus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        config.init(self.root)

    def _answer(self, tid, text="E=mc2 means mass and energy are the same stuff."):
        ops.result_answer(self.root, tid, text)


class AskTaskLifecycle(_Bus):
    def test_an_ask_task_seals_prose_and_carries_it_back(self):
        tid = ops.compose_ask_task(self.root, "grunt1", "ELI5 E=mc2")
        self._answer(tid, "Energy equals mass times c squared.")
        ops.result_done(self.root, tid, "grunt1")
        self.assertEqual(api.answer(self.root, tid),
                         "Energy equals mass times c squared.")

    def test_answer_seals_atomically_no_separate_done(self):
        # The limbo bug: a grunt staged an answer but never ran the separate
        # `result done`, so the task sat unsealed and the lead's `team wait`
        # blocked to timeout. result_answer now seals in one step.
        tid = ops.compose_ask_task(self.root, "grunt1", "ELI5 E=mc2")
        mid = ops.result_answer(self.root, tid, "Energy equals mass times c squared.")
        self.assertIsNotNone(mid)                                     # announced to the lead
        self.assertTrue(bus.result_path(self.root, tid).exists())     # sealed, no `done` needed
        self.assertFalse(bus.staging_path(self.root, tid).exists())   # staging consumed
        self.assertEqual(api.answer(self.root, tid),
                         "Energy equals mass times c squared.")
        # a redundant `done` afterwards is a harmless no-op, not an error
        self.assertIsNone(ops.result_done(self.root, tid, "grunt1"))

    def test_verify_on_an_ask_task_is_ok_with_no_citations(self):
        tid = ops.compose_ask_task(self.root, "grunt1", "ELI5 E=mc2")
        self._answer(tid)
        ops.result_done(self.root, tid, "grunt1")
        r = api.verify_task(self.root, tid)
        self.assertEqual(r.kind, "ask")
        self.assertTrue(r.ok)
        self.assertFalse(r.verifiable)
        self.assertEqual(r.verdicts, [])

    def test_wait_returns_the_answer_so_the_lead_need_not_reread(self):
        tid = ops.compose_ask_task(self.root, "grunt1", "ELI5 E=mc2")
        self._answer(tid, "the whole point")
        ops.result_done(self.root, tid, "grunt1")
        r = api.wait_tasks(self.root, [tid], timeout=1.0)
        self.assertEqual(r.sealed, [tid])
        self.assertEqual(r.answers[tid], "the whole point")
        self.assertTrue(r.ok)

    def test_an_empty_answer_will_not_stage(self):
        tid = ops.compose_ask_task(self.root, "grunt1", "q")
        with self.assertRaises(StateError):
            ops.result_answer(self.root, tid, "   \n  ")


class TheScopeFence(_Bus):
    """An ask task takes no scope. Naming a file is a claim about that file, and
    a claim is checkable -- so it is a find task. Without the fence, `ask` is a
    way to launder an unverifiable claim about the code past `verify`."""

    def test_api_send_refuses_scope_on_an_ask_task(self):
        with self.assertRaises(StateError) as cm:
            api.send(self.root, "grunt1", question="q", scope=["src"], kind="ask",
                     p=_FakePane())
        self.assertIn("no --scope", str(cm.exception))


class SealKindsDoNotCross(_Bus):
    """Each kind seals on its own evidence and refuses the other's, or `verify`
    would answer a question it was never asked."""

    def test_an_ask_task_refuses_citations(self):
        tid = ops.compose_ask_task(self.root, "grunt1", "q")
        ops.result_add(self.root, tid, {"file": "a.py", "line": 1,
                                        "symbol": "x", "evidence": "x = 1"})
        with self.assertRaises(StateError) as cm:
            ops.result_done(self.root, tid, "grunt1")
        self.assertIn("seals an answer, not citations", str(cm.exception))

    def test_a_find_task_refuses_a_prose_answer(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        # result_answer seals atomically, so a find task rejects the prose at
        # answer time -- it seals citations, not prose.
        with self.assertRaises(StateError) as cm:
            ops.result_answer(self.root, tid, "some prose")
        self.assertIn("prose is not a report", str(cm.exception))

    def test_an_empty_find_seal_still_refuses(self):
        """The old guard must survive: a find task with nothing staged does not
        seal, so a zero-citation vacuous PASS is never reachable through it."""
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        with self.assertRaises(StateError) as cm:
            ops.result_done(self.root, tid, "grunt1")
        self.assertIn("no staged records", str(cm.exception))


class VacuousPassIsClosed(_Bus):
    """`any_failed([])` is False. Once ask tasks can seal empty, result_done is
    no longer the sole guard, so verify must fail closed on an empty find."""

    def test_a_find_result_with_zero_records_is_not_ok(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        # Bypass result_done's guard the way a hand-written bus would, to prove
        # the SECOND line of defence (verify) also holds.
        bus.write_json(bus.result_path(self.root, tid),
                       {"task": tid, "records": [], "agent": "grunt1"})
        r = api.verify_task(self.root, tid)
        self.assertEqual(r.kind, "find")
        self.assertFalse(r.ok)


class BlockedWaitDoesNotDeadlock(_Bus):
    """A blocked grunt is idle at its prompt. The lead must not sleep through
    it: measured, a grunt blocked after 4s and its lead sat out 600s."""

    def _block(self, tid, agent="grunt1"):
        return ops.post_message(self.root, agent, "blocked", tid, "need a hint")

    def test_for_tasks_returns_the_blocking_message(self):
        tid = ops.compose_task(self.root, "grunt1", "q", ["src"])
        self._block(tid)
        sealed, missing, blocked = wait.for_tasks(self.root, [tid], timeout=5.0)
        self.assertEqual(sealed, [])
        self.assertEqual(missing, [])
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["task"], tid)

    def test_wait_tasks_is_not_ok_when_blocked(self):
        tid = ops.compose_task(self.root, "grunt1", "q", ["src"])
        self._block(tid)
        r = api.wait_tasks(self.root, [tid], timeout=5.0)
        self.assertFalse(r.ok)
        self.assertEqual([m["task"] for m in r.blocked], [tid])
        self.assertNotIn(tid, r.superseded)      # blocked is not superseded

    def test_a_blocked_task_returns_fast_not_at_the_deadline(self):
        """The whole point: it must not run to timeout."""
        tid = ops.compose_task(self.root, "grunt1", "q", ["src"])
        self._block(tid)
        ticks = []

        class Clock:
            def __init__(self): self.t = 0.0
            def now(self): return self.t
            def sleep(self, d): self.t += d; ticks.append(d)

        c = Clock()
        wait.for_tasks(self.root, [tid], timeout=600.0, now=c.now, sleep=c.sleep)
        self.assertLess(c.t, 600.0)              # returned early

    def test_a_failed_message_also_lifts_the_wait(self):
        tid = ops.compose_task(self.root, "grunt1", "q", ["src"])
        ops.post_message(self.root, "grunt1", "failed", tid, "compile broke")
        _, _, blocked = wait.for_tasks(self.root, [tid], timeout=5.0)
        self.assertEqual(len(blocked), 1)


class WaitExitCodeIsBlocked(_Bus):
    def test_cmd_wait_exits_5_on_a_blocked_task(self):
        from team import cli
        tid = ops.compose_task(self.root, "grunt1", "q", ["src"])
        ops.post_message(self.root, "grunt1", "blocked", tid, "need a hint")
        args = cli.build_parser().parse_args(["wait", "--task", tid, "--timeout", "5"])
        with redirect_stdout(io.StringIO()) as out:
            rc = cli.cmd_wait(args, self.root)
        self.assertEqual(rc, cli.BLOCKED)
        self.assertIn("--reply", out.getvalue())


class _FakePane:
    def exists(self, target): return True
    def wait_ready(self, target): pass
    def clear_context(self, target): pass
    def send_line(self, target, text): pass


if __name__ == "__main__":
    unittest.main()
