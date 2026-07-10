import subprocess, tempfile, unittest
from pathlib import Path

from team import worktrees


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


class WorktreesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        _git(self.root, "init", "-q", ".")
        _git(self.root, "config", "user.email", "t@t.t")
        _git(self.root, "config", "user.name", "t")
        (self.root / ".gitignore").write_text(".team/\n")
        (self.root / "a.txt").write_text("hi\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")
        self.wt = worktrees.Worktrees()

    def tearDown(self):
        self.tmp.cleanup()

    def _porcelain(self, cwd):
        p = subprocess.run(["git", "status", "--porcelain", "-uall"],
                           cwd=str(cwd), capture_output=True, text=True)
        return [l for l in p.stdout.splitlines() if l.strip()]

    def test_add_creates_a_detached_worktree_the_main_tree_cannot_see(self):
        p = self.wt.add(self.root, "grunt1")
        self.assertTrue((p / "a.txt").is_file())
        # A worktree's `.git` is a file, not a directory. This is the fact that
        # made `repo_root` resolve a grunt's bus to its own worktree.
        self.assertTrue((p / ".git").is_file())
        self.assertEqual(self._porcelain(self.root), [])

    def test_add_refuses_on_a_repo_with_no_commits(self):
        with tempfile.TemporaryDirectory() as d:
            _git(d, "init", "-q", ".")
            with self.assertRaises(worktrees.WorktreeError) as cm:
                self.wt.add(Path(d), "grunt1")
            self.assertIn("no commits", str(cm.exception))

    def test_dirty_is_empty_on_a_fresh_worktree(self):
        self.wt.add(self.root, "grunt1")
        self.assertEqual(self.wt.dirty(self.root, "grunt1"), [])

    def test_dirty_sees_a_file_nested_inside_a_new_directory(self):
        """`git status --porcelain` without -uall collapses this to `?? sub/`.
        The guard exists to notice files, so the flag is load-bearing.
        """
        p = self.wt.add(self.root, "grunt1")
        (p / "sub").mkdir()
        (p / "sub" / "Plugin.cs").write_text("class X {}\n")
        self.assertEqual(self.wt.dirty(self.root, "grunt1"),
                         ["?? sub/Plugin.cs"])

    def test_dirty_sees_a_modified_tracked_file(self):
        p = self.wt.add(self.root, "grunt1")
        (p / "a.txt").write_text("changed\n")
        self.assertEqual(self.wt.dirty(self.root, "grunt1"), [" M a.txt"])

    def test_dirty_in_one_worktree_ignores_another(self):
        """The containment property this module exists to provide."""
        self.wt.add(self.root, "grunt1")
        p2 = self.wt.add(self.root, "grunt2")
        (p2 / "noise.cs").write_text("x\n")
        self.assertEqual(self.wt.dirty(self.root, "grunt1"), [])
        self.assertEqual(self.wt.dirty(self.root, "grunt2"), ["?? noise.cs"])

    def test_remove_deletes_the_tree_and_unregisters_it(self):
        p = self.wt.add(self.root, "grunt1")
        (p / "scratch.cs").write_text("x\n")   # --force must discard this
        self.wt.remove(self.root, "grunt1")
        self.assertFalse(p.exists())
        out = subprocess.run(["git", "worktree", "list"], cwd=str(self.root),
                             capture_output=True, text=True).stdout
        self.assertNotIn("grunt1", out)

    def test_prune_repairs_a_worktree_whose_directory_vanished(self):
        import shutil
        self.wt.add(self.root, "grunt1")
        shutil.rmtree(worktrees.work_dir(self.root))   # someone `rm -rf .team`'d
        self.wt.prune(self.root)
        out = subprocess.run(["git", "worktree", "list"], cwd=str(self.root),
                             capture_output=True, text=True).stdout
        self.assertNotIn("grunt1", out)

    def test_agents_lists_existing_worktrees_only(self):
        self.assertEqual(self.wt.agents(self.root), [])
        self.wt.add(self.root, "grunt2")
        self.wt.add(self.root, "grunt1")
        self.assertEqual(self.wt.agents(self.root), ["grunt1", "grunt2"])

    def test_git_failure_is_a_worktree_error_not_a_traceback(self):
        with self.assertRaises(worktrees.WorktreeError):
            self.wt.remove(self.root, "never-existed")


if __name__ == "__main__":
    unittest.main()
