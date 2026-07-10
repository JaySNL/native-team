import subprocess, tempfile, unittest
from pathlib import Path

from team import bus, collect, config, worktrees
from team.config import StateError


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self._git("init", "-q", ".")
        self._git("config", "user.email", "t@t.t")
        self._git("config", "user.name", "t")
        (self.root / "a.txt").write_text("hi\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "init")
        config.init(self.root)
        self.wt = worktrees.Worktrees()
        self.work = self.wt.add(self.root, "grunt1")

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=str(self.root), check=True,
                       capture_output=True, text=True)

    def _snapshot(self, tid, create, agent="grunt1"):
        bus.write_json(bus.snapshot_path(self.root, tid),
                       {"task": tid, "agent": agent, "create": create})

    def _seal(self, tid):
        bus.write_json(bus.result_path(self.root, tid),
                       {"task": tid, "records": [], "agent": "grunt1"})

    def _made(self, rel, text="class X {}\n"):
        p = self.work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    # -- the happy path ----------------------------------------------------

    def test_collect_copies_declared_files_into_the_main_tree(self):
        self._snapshot("011", ["src/Plugin.cs"])
        self._seal("011")
        self._made("src/Plugin.cs")

        actions = collect.collect(self.root, "011")

        self.assertEqual(actions, ["collected src/Plugin.cs"])
        self.assertEqual((self.root / "src" / "Plugin.cs").read_text(),
                         "class X {}\n")
        # and the original is left where it was
        self.assertTrue((self.work / "src" / "Plugin.cs").is_file())

    def test_collect_copies_nothing_it_was_not_told_to(self):
        self._snapshot("011", ["Wanted.cs"])
        self._seal("011")
        self._made("Wanted.cs")
        self._made("Sneaky.cs")

        collect.collect(self.root, "011")

        self.assertTrue((self.root / "Wanted.cs").is_file())
        self.assertFalse((self.root / "Sneaky.cs").exists())

    # -- refusals ----------------------------------------------------------

    def test_collect_refuses_to_overwrite_an_existing_file(self):
        (self.root / "Plugin.cs").write_text("mine\n")
        self._snapshot("011", ["Plugin.cs"])
        self._seal("011")
        self._made("Plugin.cs")

        with self.assertRaises(StateError) as cm:
            collect.collect(self.root, "011")
        self.assertIn("refusing to overwrite", str(cm.exception))
        self.assertEqual((self.root / "Plugin.cs").read_text(), "mine\n")

    def test_a_collision_on_a_later_file_copies_none_of_them(self):
        """Check everything, then copy. A loop that copies as it goes leaves
        the first file behind when the second collides, and there is no verb
        to undo it."""
        (self.root / "B.cs").write_text("mine\n")
        self._snapshot("011", ["A.cs", "B.cs"])
        self._seal("011")
        self._made("A.cs")
        self._made("B.cs")

        with self.assertRaises(StateError):
            collect.collect(self.root, "011")
        self.assertFalse((self.root / "A.cs").exists())

    def test_collect_refuses_when_the_grunt_never_created_the_file(self):
        self._snapshot("011", ["Missing.cs"])
        self._seal("011")

        with self.assertRaises(StateError) as cm:
            collect.collect(self.root, "011")
        self.assertIn("never created it", str(cm.exception))

    def test_collect_refuses_a_task_that_has_not_sealed(self):
        """Until the grunt seals, a file on disk may be half-written."""
        self._snapshot("011", ["Plugin.cs"])
        self._made("Plugin.cs")

        with self.assertRaises(StateError) as cm:
            collect.collect(self.root, "011")
        self.assertIn("has not sealed", str(cm.exception))
        self.assertFalse((self.root / "Plugin.cs").exists())

    def test_collect_refuses_a_find_task(self):
        self._seal("011")
        with self.assertRaises(StateError) as cm:
            collect.collect(self.root, "011")
        self.assertIn("not a build task", str(cm.exception))

    def test_collect_refuses_a_path_escaping_the_worktree(self):
        self._snapshot("011", ["../../../etc/passwd"])
        self._seal("011")
        with self.assertRaises(StateError) as cm:
            collect.collect(self.root, "011")
        self.assertIn("escapes", str(cm.exception))

    def test_collect_refuses_a_symlink_pointing_out_of_the_worktree(self):
        """Containment is checked after resolve(), so a link cannot smuggle a
        path out the way a `..` cannot."""
        outside = Path(self.tmp.name).parent / "outside.cs"
        (self.work / "link").symlink_to(outside)
        self._snapshot("011", ["link"])
        self._seal("011")
        with self.assertRaises(StateError) as cm:
            collect.collect(self.root, "011")
        self.assertIn("escapes", str(cm.exception))

    def test_collect_refuses_a_malformed_snapshot(self):
        bus.write_json(bus.snapshot_path(self.root, "011"), {"task": "011"})
        self._seal("011")
        with self.assertRaises(StateError) as cm:
            collect.collect(self.root, "011")
        self.assertIn("malformed", str(cm.exception))

    def test_collect_refuses_when_the_worktree_is_gone(self):
        self._snapshot("011", ["Plugin.cs"])
        self._seal("011")
        self.wt.remove(self.root, "grunt1")
        with self.assertRaises(StateError) as cm:
            collect.collect(self.root, "011")
        self.assertIn("no worktree", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
