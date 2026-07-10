import io, shutil, subprocess, tempfile, unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from team import buildverify, bus, cli, config, ops, worktrees
from team.config import StateError

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>netstandard2.0</TargetFramework></PropertyGroup>
</Project>
"""
GOOD = "namespace P { public static class C { public static int N() => 1; } }\n"
BAD = "namespace P { public static class C { public static List<int> N() => null; } }\n"

HAVE_DOTNET = shutil.which("dotnet") is not None


class _Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self._git("init", "-q", ".")
        self._git("config", "user.email", "t@t.t")
        self._git("config", "user.name", "t")
        (self.root / ".gitignore").write_text("bin/\nobj/\nprobe/bin/\nprobe/obj/\n")
        (self.root / "probe").mkdir()
        (self.root / "probe" / "P.csproj").write_text(CSPROJ)
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

    def _send(self, create=("probe/C.cs",), **kw):
        kw.setdefault("build_dir", "probe")
        kw.setdefault("build_cmd", ["dotnet", "build", "-v", "q", "--nologo"])
        return ops.compose_build_task(self.root, "grunt1", "build it",
                                      list(create), wt=self.wt, **kw)

    def _grunt_writes(self, rel, text):
        p = self.work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class SendPreconditionTest(_Repo):
    def test_send_refuses_a_create_path_that_already_exists(self):
        self._grunt_writes("probe/C.cs", GOOD)
        with self.assertRaises(StateError) as cm:
            self._send()
        self.assertIn("never modifies an existing file", str(cm.exception))

    def test_replace_lets_the_lead_delete_it_first(self):
        self._grunt_writes("probe/C.cs", GOOD)
        self._send(replace=True)
        self.assertFalse((self.work / "probe" / "C.cs").exists())

    def test_send_refuses_when_build_output_is_not_gitignored(self):
        (self.root / ".gitignore").write_text("nothing\n")
        self._git("commit", "-qam", "unignore")
        self.wt.remove(self.root, "grunt1")
        self.work = self.wt.add(self.root, "grunt1")
        with self.assertRaises(StateError) as cm:
            self._send()
        self.assertIn("not gitignored", str(cm.exception))

    def test_send_refuses_a_create_path_escaping_the_worktree(self):
        with self.assertRaises(StateError) as cm:
            self._send(create=["../../escape.cs"])
        self.assertIn("escapes the worktree", str(cm.exception))

    def test_send_refuses_with_no_create_paths(self):
        with self.assertRaises(StateError) as cm:
            self._send(create=[])
        self.assertIn("at least one --create", str(cm.exception))

    def test_send_refuses_without_a_worktree(self):
        self.wt.remove(self.root, "grunt1")
        with self.assertRaises(StateError) as cm:
            self._send()
        self.assertIn("no worktree", str(cm.exception))

    def test_snapshot_records_intent_before_the_task_is_announced(self):
        tid = self._send()
        snap = bus.read_json(bus.snapshot_path(self.root, tid))
        self.assertEqual(snap["create"], ["probe/C.cs"])
        self.assertEqual(snap["agent"], "grunt1")
        self.assertEqual(snap["build_dir"], "probe")
        self.assertIn("dotnet", snap["build_cmd"])

    def test_the_grunt_is_told_never_to_modify_an_existing_file(self):
        tid = self._send()
        body = bus.read_json(bus.task_path(self.root, "grunt1", tid))["protocol"]
        self.assertIn("Do NOT", body)
        self.assertIn("grep -n", body)
        self.assertIn(str(self.work), body)


class EscapeTest(_Repo):
    """Task 013: qwen's WriteFile resolves against its project root and takes no
    cwd, so a pane rooted in the main tree wrote the declared file THERE -- where
    the worktree's containment check could never see it."""

    def test_a_declared_file_appearing_in_the_main_tree_is_an_escape(self):
        tid = self._send()
        self._grunt_writes("probe/C.cs", GOOD)
        (self.root / "probe" / "C.cs").write_text(GOOD)   # the 013 failure

        v = buildverify.verify_build(self.root, tid, wt=self.wt)
        self.assertEqual(v.status, "ESCAPED")
        self.assertIn("probe/C.cs", v.detail)

    def test_escape_is_reported_even_with_no_worktree_left(self):
        """A grunt with nowhere to write is exactly who writes into the main
        tree. NO_WORKTREE would hide the more important fact."""
        tid = self._send()
        (self.root / "probe" / "C.cs").write_text(GOOD)
        self.wt.remove(self.root, "grunt1")

        v = buildverify.verify_build(self.root, tid, wt=self.wt)
        self.assertEqual(v.status, "ESCAPED")

    def test_escape_is_checked_before_the_compiler_runs(self):
        calls = []

        class Spy:
            def __init__(self, real): self.real = real
            def dirty(self, r, a): return self.real.dirty(r, a)
            def build(self, *a, **k):
                calls.append(a)
                return (0, "should never run")

        tid = self._send()
        self._grunt_writes("probe/C.cs", GOOD)
        (self.root / "probe" / "C.cs").write_text(GOOD)
        self.assertEqual(
            buildverify.verify_build(self.root, tid, wt=Spy(self.wt)).status,
            "ESCAPED")
        self.assertEqual(calls, [])

    def test_send_refuses_when_the_create_path_already_exists_in_the_main_tree(self):
        """Without this, ESCAPED would fire on a file the lead put there."""
        (self.root / "probe" / "C.cs").write_text(GOOD)
        with self.assertRaises(StateError) as cm:
            self._send()
        self.assertIn("main tree", str(cm.exception))

    def test_replace_does_not_delete_the_leads_file(self):
        (self.root / "probe" / "C.cs").write_text("the lead's own work\n")
        with self.assertRaises(StateError):
            self._send(replace=True)
        self.assertEqual((self.root / "probe" / "C.cs").read_text(),
                         "the lead's own work\n")


class ProvisionedSettingsTest(_Repo):
    """The pane's cwd is the worktree, so qwen reads its settings from there.
    That file must be invisible to every check that judges the grunt."""

    def test_provisioned_settings_are_not_the_grunts_work(self):
        config.provision(self.work)
        self.assertTrue((self.work / ".qwen" / "settings.json").is_file())
        self.assertEqual(self.wt.dirty(self.root, "grunt1"), [])

    def test_provisioned_settings_do_not_fail_containment(self):
        config.provision(self.work)
        tid = self._send()
        self._grunt_writes("probe/C.cs", GOOD)
        v = buildverify.verify_build(
            self.root, tid, wt=FakeBuildTest._Wt(self.wt, 0, "ok"))
        self.assertEqual(v.status, "PASS")

    def test_provisioned_settings_do_not_block_teardown(self):
        config.provision(self.work)
        config.down(self.root, wt=self.wt)          # no --force
        self.assertFalse((self.root / ".team").exists())

    def test_uncollected_work_still_blocks_teardown(self):
        config.provision(self.work)
        self._grunt_writes("probe/C.cs", GOOD)
        with self.assertRaises(StateError):
            config.down(self.root, wt=self.wt)


class ContainmentTest(_Repo):
    def test_a_file_outside_the_declared_set_fails_containment(self):
        tid = self._send()
        self._grunt_writes("probe/C.cs", GOOD)
        self._grunt_writes("probe/Sneaky.cs", GOOD)

        v = buildverify.verify_build(self.root, tid, wt=self.wt)
        self.assertEqual(v.status, "CONTAINMENT")
        self.assertIn("Sneaky.cs", v.detail)

    def test_modifying_a_tracked_file_fails_containment(self):
        tid = self._send()
        self._grunt_writes("probe/C.cs", GOOD)
        (self.work / "a.txt").write_text("tampered\n")

        v = buildverify.verify_build(self.root, tid, wt=self.wt)
        self.assertEqual(v.status, "CONTAINMENT")
        self.assertIn("a.txt", v.detail)

    def test_a_file_nested_in_a_new_directory_is_not_hidden(self):
        """`git status --porcelain` without -uall collapses this to `?? deep/`.
        The whole check exists to see files."""
        tid = self._send()
        self._grunt_writes("probe/C.cs", GOOD)
        self._grunt_writes("deep/nested/Evil.cs", GOOD)

        v = buildverify.verify_build(self.root, tid, wt=self.wt)
        self.assertEqual(v.status, "CONTAINMENT")
        self.assertIn("Evil.cs", v.detail)

    def test_files_dirty_before_the_task_are_not_blamed_on_the_grunt(self):
        self._grunt_writes("preexisting.cs", GOOD)   # dirty *before* send
        tid = self._send()
        self._grunt_writes("probe/C.cs", GOOD)

        v = buildverify.verify_build(self.root, tid, wt=self.wt)
        self.assertNotEqual(v.status, "CONTAINMENT")

    def test_a_declared_file_never_created_is_not_created(self):
        tid = self._send()
        v = buildverify.verify_build(self.root, tid, wt=self.wt)
        self.assertEqual(v.status, "NOT_CREATED")
        self.assertIn("probe/C.cs", v.detail)

    def test_a_missing_worktree_is_reported_not_crashed(self):
        tid = self._send()
        self.wt.remove(self.root, "grunt1")
        v = buildverify.verify_build(self.root, tid, wt=self.wt)
        self.assertEqual(v.status, "NO_WORKTREE")

    def test_containment_is_checked_before_the_compiler_runs(self):
        """A build that would fail must still report CONTAINMENT: the grunt
        going out of bounds is the more important fact, and compiling is slow."""
        calls = []

        class Spy:
            def __init__(self, real): self.real = real
            def dirty(self, r, a): return self.real.dirty(r, a)
            def build(self, *a, **k):
                calls.append(a)
                return (1, "should never run")

        tid = self._send()
        self._grunt_writes("probe/C.cs", GOOD)
        self._grunt_writes("probe/Sneaky.cs", GOOD)
        v = buildverify.verify_build(self.root, tid, wt=Spy(self.wt))
        self.assertEqual(v.status, "CONTAINMENT")
        self.assertEqual(calls, [])


class FakeBuildTest(_Repo):
    """The compiler branch, without paying for a real compiler."""

    class _Wt:
        def __init__(self, real, rc, out):
            self.real, self.rc, self.out = real, rc, out
        def dirty(self, r, a): return self.real.dirty(r, a)
        def build(self, r, a, d, argv): return (self.rc, self.out)

    def test_a_failing_build_reports_the_compiler_output(self):
        tid = self._send()
        self._grunt_writes("probe/C.cs", BAD)
        v = buildverify.verify_build(
            self.root, tid, wt=self._Wt(self.wt, 1, "C.cs(1,40): error CS0246\n"))
        self.assertEqual(v.status, "BUILD_FAIL")
        self.assertIn("CS0246", v.detail)
        self.assertIn("exit 1", v.detail)

    def test_a_succeeding_build_passes(self):
        tid = self._send()
        self._grunt_writes("probe/C.cs", GOOD)
        v = buildverify.verify_build(
            self.root, tid, wt=self._Wt(self.wt, 0, "Build succeeded."))
        self.assertEqual(v.status, "PASS")
        self.assertFalse(v.failed)

    def test_compiler_output_is_truncated(self):
        tid = self._send()
        self._grunt_writes("probe/C.cs", BAD)
        noise = "\n".join(f"error line {i}" for i in range(100))
        v = buildverify.verify_build(
            self.root, tid, wt=self._Wt(self.wt, 1, noise))
        self.assertLessEqual(len(v.detail.splitlines()),
                             buildverify.MAX_DETAIL_LINES + 1)


@unittest.skipUnless(HAVE_DOTNET, "dotnet not installed")
class RealCompilerTest(_Repo):
    def test_end_to_end_a_real_build_passes_and_a_real_error_fails(self):
        tid = self._send()
        self._grunt_writes("probe/C.cs", GOOD)
        self.assertEqual(buildverify.verify_build(self.root, tid, wt=self.wt).status,
                         "PASS")

        tid2 = self._send(create=["probe/D.cs"])
        self._grunt_writes("probe/D.cs", BAD)
        v = buildverify.verify_build(self.root, tid2, wt=self.wt)
        self.assertEqual(v.status, "BUILD_FAIL")
        self.assertIn("CS0246", v.detail)


class StaleScopeTest(_Repo):
    """A grunt in a worktree reads HEAD. `verify` reads the main tree. Dispatch
    a find task over a file that differs and the grunt cites the file it read
    while verify calls the citation fabricated."""

    def test_a_dirty_scope_path_is_refused(self):
        (self.root / "a.txt").write_text("edited, not committed\n")
        with self.assertRaises(StateError) as cm:
            ops.compose_task(self.root, "grunt1", "where?", ["a.txt"], wt=self.wt)
        self.assertIn("a.txt", str(cm.exception))
        self.assertIn("--allow-dirty", str(cm.exception))

    def test_an_untracked_scope_path_is_refused(self):
        (self.root / "new.txt").write_text("never committed\n")
        with self.assertRaises(StateError):
            ops.compose_task(self.root, "grunt1", "where?", ["new.txt"], wt=self.wt)

    def test_allow_dirty_dispatches_anyway(self):
        (self.root / "a.txt").write_text("edited, not committed\n")
        tid = ops.compose_task(self.root, "grunt1", "where?", ["a.txt"],
                               allow_dirty=True, wt=self.wt)
        self.assertTrue(bus.task_path(self.root, "grunt1", tid).is_file())

    def test_a_clean_scope_path_dispatches(self):
        tid = ops.compose_task(self.root, "grunt1", "where?", ["a.txt"], wt=self.wt)
        self.assertTrue(bus.task_path(self.root, "grunt1", tid).is_file())

    def test_no_scope_means_nothing_to_check(self):
        (self.root / "a.txt").write_text("edited, not committed\n")
        ops.compose_task(self.root, "grunt1", "where?", [], wt=self.wt)

    def test_without_a_worktree_the_grunt_reads_the_live_tree(self):
        """No worktree -> the pane fell back to the main root -> it reads the
        lead's actual file, and there is nothing stale to refuse."""
        self.wt.remove(self.root, "grunt1")
        (self.root / "a.txt").write_text("edited, not committed\n")
        ops.compose_task(self.root, "grunt1", "where?", ["a.txt"], wt=self.wt)


class CliDispatchTest(_Repo):
    """`team verify <tid>` must notice the task is a build task. Without the
    dispatch it reads .team/results/<tid>.json, which a build task need not
    have, and reports a missing file instead of a containment breach."""

    def _verify(self, *extra):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            code = cli.main(["--root", str(self.root), "verify", self.tid, *extra])
        return code, out.getvalue()

    def test_verify_reports_a_build_task_and_fails_closed(self):
        self.tid = self._send()          # declared, never created
        code, out = self._verify()
        self.assertEqual(code, cli.VERIFY_FAIL)
        self.assertIn("build", out)
        self.assertIn("NOT_CREATED", out)

    def test_lenient_still_exits_zero_on_a_build_task(self):
        self.tid = self._send()
        code, out = self._verify("--lenient")
        self.assertEqual(code, cli.OK)
        self.assertIn("NOT_CREATED", out)

    def test_a_passing_build_task_exits_zero(self):
        self.tid = self._send()
        self._grunt_writes("probe/C.cs", GOOD)
        if not HAVE_DOTNET:
            self.skipTest("dotnet not installed")
        code, out = self._verify()
        self.assertEqual(code, cli.OK)
        self.assertIn("PASS", out)


if __name__ == "__main__":
    unittest.main()
