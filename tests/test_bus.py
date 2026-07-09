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

    def test_alloc_id_ignores_superscript_digit_file(self):
        """Stray file named ² (superscript digit, isdigit()=True but int() fails) does not crash."""
        ids = bus.team_dir(self.root) / "ids"
        (ids / "²").touch()
        # Should not raise ValueError, and should return "001"
        result = bus.alloc_id(self.root)
        self.assertEqual(result, "001")

    def test_alloc_id_ignores_fullwidth_digit_file(self):
        """Stray file named １２３ (fullwidth Unicode digits) is ignored; returns "001" not "124"."""
        ids = bus.team_dir(self.root) / "ids"
        (ids / "１２３").touch()
        # Should ignore the fullwidth file and return "001"
        result = bus.alloc_id(self.root)
        self.assertEqual(result, "001")

    def test_alloc_id_with_real_ids_001_002(self):
        """Regression: with real ids 001 and 002 present, alloc_id returns "003"."""
        ids = bus.team_dir(self.root) / "ids"
        (ids / "001").touch()
        (ids / "002").touch()
        result = bus.alloc_id(self.root)
        self.assertEqual(result, "003")

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

    def test_open_task_skips_malformed_json_file(self):
        """Malformed (non-JSON) file in inbox doesn't raise; valid task still found."""
        inbox = bus.team_dir(self.root) / "inbox" / "grunt1"
        inbox.mkdir(parents=True)
        # Write a malformed file with name sorted before valid task
        (inbox / "000-bad.json").write_text("not valid json {{{")
        # Write a valid task
        bus.write_json(bus.task_path(self.root, "grunt1", "007"),
                       {"id": "007", "kind": "task"})
        # Should skip the malformed file and return the valid task
        self.assertEqual(bus.open_task(self.root, "grunt1"), "007")

    def test_open_task_skips_file_without_kind_field(self):
        """A .json file that parses but has no 'kind' field is skipped."""
        bus.write_json(bus.task_path(self.root, "grunt1", "008"),
                       {"id": "008", "some_field": "value"})
        self.assertIsNone(bus.open_task(self.root, "grunt1"))

    def test_open_task_skips_json_array_file(self):
        """A .json file containing a JSON array (not an object) is skipped."""
        inbox = bus.team_dir(self.root) / "inbox" / "grunt1"
        inbox.mkdir(parents=True)
        # Write a JSON array
        (inbox / "array.json").write_text(json.dumps([1, 2, 3]))
        self.assertIsNone(bus.open_task(self.root, "grunt1"))

    def test_open_task_derives_id_from_filename(self):
        """open_task returns task id from filename even when embedded 'id' is absent."""
        bus.write_json(bus.task_path(self.root, "grunt1", "009"),
                       {"kind": "task"})  # no "id" field
        self.assertEqual(bus.open_task(self.root, "grunt1"), "009")

    def test_open_task_skips_notes_json_invalid_id_format(self):
        """File named notes.json (not zero-padded 3-digit) containing valid task JSON is skipped."""
        inbox = bus.team_dir(self.root) / "inbox" / "grunt1"
        inbox.mkdir(parents=True)
        (inbox / "notes.json").write_text(json.dumps({"kind": "task"}))
        self.assertIsNone(bus.open_task(self.root, "grunt1"))

    def test_open_task_skips_single_digit_id_format(self):
        """File named 1.json (not zero-padded) containing valid task JSON is skipped."""
        inbox = bus.team_dir(self.root) / "inbox" / "grunt1"
        inbox.mkdir(parents=True)
        (inbox / "1.json").write_text(json.dumps({"kind": "task"}))
        self.assertIsNone(bus.open_task(self.root, "grunt1"))

    def test_open_task_skips_four_digit_id_format(self):
        """File named 0007.json (four digits instead of three) containing valid task JSON is skipped."""
        inbox = bus.team_dir(self.root) / "inbox" / "grunt1"
        inbox.mkdir(parents=True)
        (inbox / "0007.json").write_text(json.dumps({"kind": "task"}))
        self.assertIsNone(bus.open_task(self.root, "grunt1"))

    def test_open_task_accepts_valid_three_digit_zero_padded_id(self):
        """File named 007.json (valid zero-padded 3-digit format) containing task JSON is found."""
        inbox = bus.team_dir(self.root) / "inbox" / "grunt1"
        inbox.mkdir(parents=True)
        (inbox / "007.json").write_text(json.dumps({"kind": "task"}))
        self.assertEqual(bus.open_task(self.root, "grunt1"), "007")

    def test_open_task_skips_fullwidth_digit_id(self):
        """File named １２３.json (fullwidth Unicode digits) containing valid task JSON is skipped."""
        inbox = bus.team_dir(self.root) / "inbox" / "grunt1"
        inbox.mkdir(parents=True)
        (inbox / "１２３.json").write_text(json.dumps({"kind": "task"}))
        self.assertIsNone(bus.open_task(self.root, "grunt1"))

    def test_open_task_skips_arabic_indic_digit_id(self):
        """File named १२३.json (Arabic-Indic Unicode digits) containing valid task JSON is skipped."""
        inbox = bus.team_dir(self.root) / "inbox" / "grunt1"
        inbox.mkdir(parents=True)
        (inbox / "١٢٣.json").write_text(json.dumps({"kind": "task"}))
        self.assertIsNone(bus.open_task(self.root, "grunt1"))

    def test_open_task_regression_ascii_digits_still_work(self):
        """Regression: File named 042.json (ASCII digits) containing task JSON is still found."""
        inbox = bus.team_dir(self.root) / "inbox" / "grunt1"
        inbox.mkdir(parents=True)
        (inbox / "042.json").write_text(json.dumps({"kind": "task"}))
        self.assertEqual(bus.open_task(self.root, "grunt1"), "042")


if __name__ == "__main__":
    unittest.main()
