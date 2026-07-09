import json, os, tempfile, unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from team import bus


class BusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()
        (self.root / ".team" / "ids").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_root_walks_up(self):
        deep = self.root / "a" / "b"
        deep.mkdir(parents=True)
        self.assertEqual(bus.repo_root(deep), self.root)

    def test_repo_root_raises_without_git(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(bus.BusError):
                bus.repo_root(Path(d))

    def test_atomic_write_leaves_no_partial_file(self):
        p = self.root / "sub" / "x.json"
        bus.write_json(p, {"a": 1})
        self.assertEqual(bus.read_json(p), {"a": 1})
        self.assertEqual([q.name for q in p.parent.iterdir()], ["x.json"])

    def test_alloc_id_is_zero_padded_and_sequential(self):
        self.assertEqual(bus.alloc_id(self.root), "001")
        self.assertEqual(bus.alloc_id(self.root), "002")

    def test_alloc_id_is_race_safe(self):
        with ThreadPoolExecutor(max_workers=8) as ex:
            ids = list(ex.map(lambda _: bus.alloc_id(self.root), range(40)))
        self.assertEqual(len(set(ids)), 40)

    def test_open_task_none_when_result_sealed(self):
        bus.write_json(bus.task_path(self.root, "grunt1", "001"),
                       {"id": "001", "kind": "task"})
        self.assertEqual(bus.open_task(self.root, "grunt1"), "001")
        bus.write_json(bus.result_path(self.root, "001"), {"task": "001"})
        self.assertIsNone(bus.open_task(self.root, "grunt1"))

    def test_open_task_ignores_replies_and_dead_tasks(self):
        bus.write_json(bus.task_path(self.root, "grunt1", "005"),
                       {"id": "005", "kind": "reply"})
        self.assertIsNone(bus.open_task(self.root, "grunt1"))
        bus.write_json(bus.task_path(self.root, "grunt1", "006"),
                       {"id": "006", "kind": "task"})
        bus.mark_dead(self.root, "006")
        self.assertTrue(bus.is_dead(self.root, "006"))
        self.assertIsNone(bus.open_task(self.root, "grunt1"))


if __name__ == "__main__":
    unittest.main()
