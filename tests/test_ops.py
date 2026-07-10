# tests/test_ops.py
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from team import bus, config, ops, schema

REC = {"file": "a.py", "line": 1, "symbol": "x", "evidence": "x = 1"}


class OpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()
        config.init(self.root)
        (bus.team_dir(self.root) / "inbox" / "grunt1").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_compose_task_embeds_protocol_and_scope(self):
        tid = ops.compose_task(self.root, "grunt1", "where is TryHeal?", ["src/A.cs"])
        task = bus.read_json(bus.task_path(self.root, "grunt1", tid))
        self.assertEqual(task["kind"], "task")
        self.assertEqual(task["scope"], ["src/A.cs"])
        self.assertIn("team result add", task["protocol"])
        self.assertIn("team msg --blocked", task["protocol"])
        self.assertIn(tid, task["protocol"])

    def test_compose_task_refuses_second_open_task(self):
        ops.compose_task(self.root, "grunt1", "q1", [])
        with self.assertRaises(config.StateError):
            ops.compose_task(self.root, "grunt1", "q2", [])

    def test_supersede_kills_old_task_and_allows_new(self):
        old = ops.compose_task(self.root, "grunt1", "q1", [])
        new = ops.compose_task(self.root, "grunt1", "q2", [], supersede=True)
        self.assertTrue(bus.is_dead(self.root, old))
        self.assertNotEqual(old, new)

    def test_result_done_rejected_for_superseded_task(self):
        old = ops.compose_task(self.root, "grunt1", "q1", [])
        ops.compose_task(self.root, "grunt1", "q2", [], supersede=True)
        ops.result_add(self.root, old, dict(REC))
        with self.assertRaises(config.StateError):
            ops.result_done(self.root, old, "grunt1")

    def test_result_add_validates(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        with self.assertRaises(schema.SchemaError):
            ops.result_add(self.root, tid, dict(REC, evidence="  "))

    def test_result_done_seals_then_announces(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        ops.result_add(self.root, tid, dict(REC))
        mid = ops.result_done(self.root, tid, "grunt1")
        self.assertTrue(bus.result_path(self.root, tid).exists())
        self.assertFalse(bus.staging_path(self.root, tid).exists())
        msg = bus.read_json(bus.lead_inbox(self.root) / f"{mid}.json")
        self.assertEqual(msg["type"], "result")
        self.assertEqual(msg["task"], tid)

    def test_result_done_is_write_once(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        ops.result_add(self.root, tid, dict(REC))
        ops.result_done(self.root, tid, "grunt1")
        ops.result_add(self.root, tid, dict(REC))
        with self.assertRaises(config.StateError):
            ops.result_done(self.root, tid, "grunt1")

    def test_reply_requires_prior_blocked_message(self):
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        with self.assertRaises(config.StateError):
            ops.reply(self.root, "grunt1", "001", "an answer")
        mid = ops.post_message(self.root, "grunt1", "blocked", tid, "why?")
        rid = ops.reply(self.root, "grunt1", mid, "because")
        obj = bus.read_json(bus.task_path(self.root, "grunt1", rid))
        self.assertEqual(obj["kind"], "reply")

    def test_post_message_validates_body_size(self):
        with self.assertRaises(schema.SchemaError):
            ops.post_message(self.root, "grunt1", "note", "001", "x" * 2000)

    # --- Additional tests added during the mutation sweep (see task-8-report.md) ---

    def test_reply_rejected_after_non_blocked_message(self):
        # A loosened guard that only checks "was there ever a message" (and
        # not "was the *last* one 'blocked'") would wrongly let this through.
        # A `note` means the grunt is still working -- replying to it would
        # deliver a leading Escape into a busy pane and cancel its turn.
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        mid = ops.post_message(self.root, "grunt1", "note", tid, "still working")
        with self.assertRaises(config.StateError):
            ops.reply(self.root, "grunt1", mid, "ok")

    def test_result_done_seal_precedes_announce(self):
        # Patch post_message (the announce step) to snapshot on-disk state at
        # the instant it is invoked. If seal-then-announce were reversed, the
        # result file would not yet exist and the staging file would not yet
        # be gone when the announcement is written.
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        ops.result_add(self.root, tid, dict(REC))

        seen = {}
        real_post_message = ops.post_message

        def spy(root, sender, mtype, task, body):
            seen["result_exists"] = bus.result_path(root, tid).exists()
            seen["staging_gone"] = not bus.staging_path(root, tid).exists()
            return real_post_message(root, sender, mtype, task, body)

        with mock.patch("team.ops.post_message", side_effect=spy):
            ops.result_done(self.root, tid, "grunt1")

        self.assertTrue(seen["result_exists"])
        self.assertTrue(seen["staging_gone"])

    def test_result_done_revalidates_hand_written_staging(self):
        # result_add validates on the way in, but a grunt could bypass it and
        # write staging/NNN.json directly. result_done must independently
        # re-validate every record before sealing, and must not seal a
        # partial/invalid batch.
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        bus.write_json(bus.staging_path(self.root, tid),
                        {"task": tid, "records": [dict(REC, evidence="   ")]})
        with self.assertRaises(schema.SchemaError):
            ops.result_done(self.root, tid, "grunt1")
        self.assertFalse(bus.result_path(self.root, tid).exists())

    def test_last_message_from_orders_by_id_not_mtime(self):
        # Write the higher-id message first (earlier mtime) and the lower-id
        # message second (later mtime). Ordering by mtime would return the
        # lower id; ordering by filename/id (what the bus guarantees
        # elsewhere) returns the higher one.
        tid = ops.compose_task(self.root, "grunt1", "q", [])
        box = bus.lead_inbox(self.root)
        bus.write_json(box / "005.json", {
            "id": "005", "from": "grunt1", "type": "note",
            "task": tid, "body": "written first, higher id",
        })
        time.sleep(0.01)
        bus.write_json(box / "002.json", {
            "id": "002", "from": "grunt1", "type": "blocked",
            "task": tid, "body": "written second, lower id",
        })
        last = ops.last_message_from(self.root, "grunt1")
        self.assertEqual(last["id"], "005")


if __name__ == "__main__":
    unittest.main()
