import tempfile, unittest
from pathlib import Path

from team import bus, config, ops, wait


class FakeClock:
    """Deterministic clock: every sleep advances time; a hook fires once."""

    def __init__(self, on_tick=None, fire_after=1):
        self.t = 0.0
        self.ticks = 0
        self.on_tick = on_tick
        self.fire_after = fire_after

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        self.ticks += 1
        if self.on_tick and self.ticks == self.fire_after:
            self.on_tick()


class WaitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()
        config.init(self.root)
        (bus.team_dir(self.root) / "inbox" / "grunt1").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_for_lead_ignores_preexisting_messages(self):
        ops.post_message(self.root, "grunt1", "note", "001", "stale")
        clock = FakeClock()
        got = wait.for_lead(self.root, timeout=1.0, now=clock.now, sleep=clock.sleep)
        self.assertEqual(got, [])

    def test_for_lead_returns_only_new_messages(self):
        ops.post_message(self.root, "grunt1", "note", "001", "stale")
        clock = FakeClock(on_tick=lambda: ops.post_message(
            self.root, "grunt1", "blocked", "001", "fresh"))
        got = wait.for_lead(self.root, timeout=10.0, now=clock.now, sleep=clock.sleep)
        self.assertEqual([m["body"] for m in got], ["fresh"])

    def test_for_tasks_reports_sealed_and_missing(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        ops.result_add(self.root, tid, {"file": "a.py", "line": 1,
                                        "symbol": "x", "evidence": "x = 1"})
        clock = FakeClock(on_tick=lambda: ops.result_done(self.root, tid, "grunt1"))
        sealed, missing = wait.for_tasks(self.root, [tid, "999"], timeout=2.0,
                                         now=clock.now, sleep=clock.sleep)
        self.assertEqual(sealed, [tid])
        self.assertEqual(missing, ["999"])

    def test_for_tasks_treats_dead_task_as_resolved(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        bus.mark_dead(self.root, tid)
        clock = FakeClock()
        sealed, missing = wait.for_tasks(self.root, [tid], timeout=1.0,
                                         now=clock.now, sleep=clock.sleep)
        self.assertEqual((sealed, missing), ([], []))


if __name__ == "__main__":
    unittest.main()
