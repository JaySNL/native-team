import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from team import bus, config, worktrees


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def qwen(self):
        return self.root / ".qwen" / "settings.json"

    def backup(self):
        return self.root / ".qwen" / "settings.json.team-backup"

    # -- GRUNT_SETTINGS: the exact payload, pinned independently of the source --

    def test_grunt_settings_constant_matches_spec(self):
        # Hardcoded expected value (not a reference to config.GRUNT_SETTINGS):
        # dropping a key here would be a no-op test if we compared the constant
        # to itself. This pins the literal probe-derived payload from the brief.
        expected = {
            "context": {"fileName": ["TEAM_GRUNT_CONTEXT.md"]},
            "tools": {
                "approvalMode": "yolo",
                "computerUse": {"enabled": False},
                "excludeTools": ["write_file", "replace", "edit", "save_memory", "web_fetch"],
            },
            "memory": {"enableAutoSkill": False},
            "skills": {"disabled": [
                "hyperframes", "hyperframes-animation", "hyperframes-cli",
                "hyperframes-core", "hyperframes-creative", "hyperframes-keyframes",
                "hyperframes-registry", "media-use",
            ]},
            "model": {
                "name": "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-dwq-v2",
                "sessionTokenLimit": 200000,
                "maxSessionTurns": -1,
                "maxWallTimeSeconds": 900,
            },
        }
        self.assertEqual(config.GRUNT_SETTINGS, expected)

    # -- grunt_settings(): env decoupling, defaults preserve the constant --

    def test_grunt_settings_defaults_equal_constant(self):
        # Empty env -> byte-for-byte the pinned constant, so the author's rig and
        # every provenance check are untouched when no TEAM_GRUNT_* is set.
        self.assertEqual(config.grunt_settings(env={}), config.GRUNT_SETTINGS)
        self.assertNotIn("modelProviders", config.grunt_settings(env={}))

    def test_grunt_settings_model_and_caps_override(self):
        s = config.grunt_settings(env={
            "TEAM_GRUNT_MODEL": "ollama/qwen3-coder:30b",
            "TEAM_GRUNT_SESSION_TOKEN_LIMIT": "64000",
            "TEAM_GRUNT_WALL_SECONDS": "600",
        })
        self.assertEqual(s["model"]["name"], "ollama/qwen3-coder:30b")
        self.assertEqual(s["model"]["sessionTokenLimit"], 64000)
        self.assertEqual(s["model"]["maxWallTimeSeconds"], 600)
        # no base url -> still no provider block
        self.assertNotIn("modelProviders", s)

    def test_grunt_settings_base_url_writes_self_contained_provider(self):
        s = config.grunt_settings(env={
            "TEAM_GRUNT_MODEL": "qwen3-coder:30b",
            "TEAM_GRUNT_BASE_URL": "http://localhost:11434/v1",
            "TEAM_GRUNT_CONTEXT_WINDOW": "40960",
        })
        prov = s["modelProviders"]["openai"][0]
        self.assertEqual(prov["id"], "qwen3-coder:30b")
        self.assertEqual(prov["name"], "qwen3-coder:30b")
        self.assertEqual(prov["baseUrl"], "http://localhost:11434/v1")
        # envKey names an env var, never the key itself
        self.assertEqual(prov["envKey"], config.GRUNT_API_KEY_ENV)
        self.assertEqual(prov["generationConfig"]["contextWindowSize"], 40960)
        # temperature 0 for grunt determinism, as honored extra_body
        self.assertEqual(prov["generationConfig"]["extra_body"]["temperature"], 0)

    def test_grunt_settings_does_not_mutate_constant(self):
        before = copy.deepcopy(config.GRUNT_SETTINGS)
        config.grunt_settings(env={"TEAM_GRUNT_MODEL": "x", "TEAM_GRUNT_BASE_URL": "http://h/v1"})
        self.assertEqual(config.GRUNT_SETTINGS, before)

    # -- grunt_backend_status(): first-launch guidance --

    def test_grunt_backend_status_env_wins(self):
        st = config.grunt_backend_status(env={"TEAM_GRUNT_BASE_URL": "http://h/v1"})
        self.assertEqual(st, ("env", "http://h/v1"))

    def test_grunt_backend_status_none_then_global(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"HOME": home}, clear=False):
                # no global CLI config -> setup needed
                self.assertEqual(config.grunt_backend_status(env={}), ("none", None))
                self.assertIn("SETUP NEEDED", config._grunt_backend_note(env={}))
                # a global qwen profile appears -> use it, model surfaced
                qdir = Path(home) / ".qwen"
                qdir.mkdir()
                (qdir / "settings.json").write_text(json.dumps(
                    {"modelProviders": {"openai": [{"name": "coder"}]}, "model": {"name": "coder"}}))
                self.assertEqual(config.grunt_backend_status(env={}), ("global", "coder"))
                note = config._grunt_backend_note(env={})
                self.assertIn("global ~/.qwen", note)
                # reports the actual pinned grunt model, not the caller's global default
                self.assertIn(config.grunt_settings(env={})["model"]["name"], note)
                self.assertIn("TEAM_GRUNT_MODEL", note)  # can point a CLI at a different model

    # -- init: bus tree --

    def test_init_creates_bus_dirs(self):
        config.init(self.root)
        for sub in ("inbox/lead", "results", "staging", "logs", "ids", "dead"):
            self.assertTrue((self.root / ".team" / sub).is_dir(), sub)

    def test_init_writes_roster_json_empty(self):
        config.init(self.root)
        self.assertEqual(bus.read_json(self.root / ".team" / "roster.json"), {})

    def test_init_writes_full_grunt_settings(self):
        # Pin the exact written payload under a clean env, so a developer who has
        # exported TEAM_GRUNT_* does not turn this drift-guard into a false red.
        clean = {k: v for k, v in os.environ.items() if not k.startswith("TEAM_GRUNT_")}
        with patch.dict(os.environ, clean, clear=True):
            config.init(self.root)
        got = json.loads(self.qwen().read_text())
        expected = {
            "context": {"fileName": ["TEAM_GRUNT_CONTEXT.md"]},
            "tools": {
                "approvalMode": "yolo",
                "computerUse": {"enabled": False},
                "excludeTools": ["write_file", "replace", "edit", "save_memory", "web_fetch"],
            },
            "memory": {"enableAutoSkill": False},
            "skills": {"disabled": [
                "hyperframes", "hyperframes-animation", "hyperframes-cli",
                "hyperframes-core", "hyperframes-creative", "hyperframes-keyframes",
                "hyperframes-registry", "media-use",
            ]},
            "model": {
                "name": "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit-dwq-v2",
                "sessionTokenLimit": 200000,
                "maxSessionTurns": -1,
                "maxWallTimeSeconds": 900,
            },
        }
        self.assertEqual(got, expected)

    def test_init_backs_up_existing_settings(self):
        self.qwen().parent.mkdir(parents=True)
        self.qwen().write_text('{"mine": true}')
        config.init(self.root)
        self.assertEqual(json.loads(self.backup().read_text()), {"mine": True})

    def test_init_appends_gitignore_entries_once(self):
        config.init(self.root)
        config.down(self.root)
        config.init(self.root)
        text = (self.root / ".gitignore").read_text()
        self.assertEqual(text.count(".team*/"), 1)
        self.assertEqual(text.count(".qwen/"), 1)

    def test_init_refuses_stale_bus_without_force(self):
        config.init(self.root)
        with self.assertRaises(config.StateError):
            config.init(self.root)
        config.init(self.root, force=True)  # must not raise

    def test_init_force_is_idempotent(self):
        config.init(self.root)
        first = sorted(str(p.relative_to(self.root)) for p in (self.root / ".team").rglob("*"))
        first_init_json = bus.read_json(self.root / ".team" / "init.json")
        config.init(self.root, force=True)
        second = sorted(str(p.relative_to(self.root)) for p in (self.root / ".team").rglob("*"))
        second_init_json = bus.read_json(self.root / ".team" / "init.json")
        self.assertEqual(first, second)
        self.assertEqual(first_init_json, second_init_json)

    def test_init_returns_hijack_warning(self):
        warnings = config.init(self.root)
        self.assertTrue(any("YOLO" in w for w in warnings))

    # -- init: refusing to clobber a stale settings backup --

    def test_init_refuses_stale_settings_backup_without_force(self):
        self.qwen().parent.mkdir(parents=True)
        self.qwen().write_text('{"mine": true}')
        self.backup().write_text('{"original": true}')
        with self.assertRaises(config.StateError):
            config.init(self.root)
        # Neither file was touched.
        self.assertEqual(json.loads(self.qwen().read_text()), {"mine": True})
        self.assertEqual(json.loads(self.backup().read_text()), {"original": True})

    def test_init_force_overrides_stale_settings_backup(self):
        self.qwen().parent.mkdir(parents=True)
        self.qwen().write_text('{"mine": true}')
        self.backup().write_text('{"original": true}')
        config.init(self.root, force=True)  # must not raise
        got = json.loads(self.qwen().read_text())
        self.assertEqual(got["tools"]["approvalMode"], "yolo")

    # -- down --

    def test_down_restores_backup(self):
        self.qwen().parent.mkdir(parents=True)
        self.qwen().write_text('{"mine": true}')
        config.init(self.root)
        config.down(self.root)
        self.assertEqual(json.loads(self.qwen().read_text()), {"mine": True})
        self.assertFalse((self.root / ".team").exists())

    def test_down_removes_settings_it_created(self):
        config.init(self.root)
        config.down(self.root)
        self.assertFalse(self.qwen().exists())

    def test_down_noop_on_absent_bus(self):
        # No .team, no .qwen at all: must succeed quietly, not raise.
        actions = config.down(self.root)
        self.assertEqual(actions, [])
        self.assertFalse((self.root / ".team").exists())
        self.assertFalse(self.qwen().exists())

    def test_down_after_force_reinit_still_restores_true_original(self):
        self.qwen().parent.mkdir(parents=True)
        self.qwen().write_text('{"mine": true}')
        config.init(self.root)
        config.init(self.root, force=True)  # re-init over our own bus, no down() in between
        config.down(self.root)
        self.assertEqual(json.loads(self.qwen().read_text()), {"mine": True})

    # -- SAFETY: never delete anything not proven to be <repo_root>/.team --

    def test_down_refuses_team_dir_that_is_symlink_outside_repo(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "evil"
        target.mkdir()
        canary = target / "canary.txt"
        canary.write_text("keep me")

        (self.root / ".team").symlink_to(target)

        with self.assertRaises(config.StateError):
            config.down(self.root)

        self.assertTrue(canary.exists())
        self.assertTrue(target.exists())
        self.assertTrue((self.root / ".team").is_symlink())

    def test_init_force_refuses_team_dir_that_is_symlink_outside_repo(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "evil"
        target.mkdir()
        canary = target / "canary.txt"
        canary.write_text("keep me")

        (self.root / ".team").symlink_to(target)

        with self.assertRaises(config.StateError):
            config.init(self.root, force=True)

        self.assertTrue(canary.exists())
        self.assertTrue(target.exists())

    def test_down_refuses_when_team_dir_resolves_outside_repo_root(self):
        # Not a symlink at the .team level -- simulates a bug/adversarial
        # bus.team_dir() that hands back a directory named exactly ".team"
        # (so a name-only check would be fooled) but physically outside the
        # repo. Only the parent-containment check catches this.
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        external_team = Path(outside.name) / ".team"
        external_team.mkdir()
        canary = external_team / "canary.txt"
        canary.write_text("keep me")

        with patch.object(config.bus, "team_dir", return_value=external_team):
            with self.assertRaises(config.StateError):
                config.down(self.root)

        self.assertTrue(canary.exists())
        self.assertTrue(external_team.exists())

    def test_down_refuses_an_in_repo_directory_not_named_dot_team(self):
        # Isolates the name check. A real directory, not a symlink, physically
        # inside the repo -- so the symlink refusal and the parent-containment
        # check both pass. Only `resolved.name != ".team"` stands between a
        # buggy bus.team_dir() and rmtree'ing the user's source tree.
        victim = self.root / "src"
        victim.mkdir()
        canary = victim / "canary.txt"
        canary.write_text("keep me")

        with patch.object(config.bus, "team_dir", return_value=victim):
            with self.assertRaisesRegex(config.StateError, "not '.team'"):
                config.down(self.root)

        self.assertEqual(canary.read_text(), "keep me")
        self.assertTrue(victim.exists())

    def test_down_refuses_team_dir_symlinked_to_a_team_named_dir_inside_repo(self):
        # The symlink target is named exactly ".team" and lives *inside* the
        # repo (root/elsewhere/.team), so both the name check and the
        # parent-containment check would wrongly pass this. Only the
        # unconditional "refuse any symlink" rule catches it.
        real_target = self.root / "elsewhere" / ".team"
        real_target.mkdir(parents=True)
        canary = real_target / "canary.txt"
        canary.write_text("keep me")

        (self.root / ".team").symlink_to(real_target)

        with self.assertRaises(config.StateError):
            config.down(self.root)

        self.assertTrue(canary.exists())
        self.assertTrue(real_target.exists())

    def test_init_force_refuses_when_team_dir_resolves_outside_repo_root(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        external_team = Path(outside.name) / ".team"
        external_team.mkdir()
        canary = external_team / "canary.txt"
        canary.write_text("keep me")

        with patch.object(config.bus, "team_dir", return_value=external_team):
            with self.assertRaises(config.StateError):
                config.init(self.root, force=True)

        self.assertTrue(canary.exists())
        self.assertTrue(external_team.exists())

    def test_down_removes_the_bus_at_cwd_even_when_cwd_is_not_a_git_repo(self):
        """The reported bug: `team down` in ~/teamTest -- a dir that holds a bus
        but is not its own git repo, nested under a parent git repo -- resolved
        the boundary UP to the parent and tore down the wrong (or no) bus, leaving
        ~/teamTest/.team on disk while the lead narrated success over empty output.
        The guard now bounds on the invocation root (cwd), never a walked-up repo,
        so the bus at cwd is removed whether or not cwd is its own git repo.

        `self.root` stands in for the parent repo (it has a `.git`); `sub` is the
        bus dir with none of its own."""
        sub = self.root / "teamTest"
        sub.mkdir()
        self.assertFalse((sub / ".git").exists())          # not its own repo
        list(config.init(sub))
        self.assertTrue((sub / ".team").is_dir())
        actions = config.down(sub)
        self.assertFalse((sub / ".team").exists())          # the REAL bus is gone
        self.assertIn(f"removed {sub / '.team'}", " ".join(actions))
        self.assertFalse((self.root / ".team").exists())    # parent never touched


if __name__ == "__main__":
    unittest.main()


class LostProvenanceTest(unittest.TestCase):
    """`rm -rf .team` (instead of `team down`) destroys init.json. The next
    init must not mistake its own GRUNT_SETTINGS for user content, or `down`
    leaves a YOLO grunt config behind in the user's repo. Observed live.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".git").mkdir()

    def test_reinit_after_manual_team_removal_leaves_no_settings_behind(self):
        config.init(self.root)                      # user had no settings.json
        shutil.rmtree(bus.team_dir(self.root))      # provenance destroyed
        config.init(self.root)
        config.down(self.root)
        self.assertFalse((self.root / ".qwen" / "settings.json").exists(),
                         "team down left our YOLO settings in the repo")
        self.assertFalse((self.root / ".qwen" / "settings.json.team-backup").exists())

    def test_real_user_settings_still_survive_the_same_dance(self):
        q = self.root / ".qwen"
        q.mkdir()
        (q / "settings.json").write_text(json.dumps({"mine": True}))
        config.init(self.root)
        shutil.rmtree(bus.team_dir(self.root))
        config.init(self.root, force=True)
        config.down(self.root)
        self.assertEqual(json.loads((q / "settings.json").read_text()), {"mine": True})


class _RealRepo(unittest.TestCase):
    """Worktree behaviour needs a real git repo; the rest of this file fakes
    `.git` with a bare directory."""

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
        bus.write_json(bus.team_dir(self.root) / "roster.json",
                       {"lead": {"pane": "0"}, "grunt1": {"pane": "1"}})
        self.wt = worktrees.Worktrees()

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=str(self.root), check=True,
                       capture_output=True, text=True)

    def _registered(self):
        out = subprocess.run(["git", "worktree", "list"], cwd=str(self.root),
                             capture_output=True, text=True).stdout
        return out


class DownWorktreeTest(_RealRepo):
    def test_down_refuses_when_a_worktree_holds_uncollected_work(self):
        p = self.wt.add(self.root, "grunt1")
        (p / "Plugin.cs").write_text("class X {}\n")

        with self.assertRaises(config.StateError) as cm:
            config.down(self.root)

        msg = str(cm.exception)
        self.assertIn("grunt1", msg)
        self.assertIn("Plugin.cs", msg)
        self.assertIn("--force", msg)
        # Nothing was destroyed: the refusal happens before any deletion.
        self.assertTrue(p.is_file() or (p / "Plugin.cs").is_file())
        self.assertTrue(bus.team_dir(self.root).is_dir())

    def test_down_force_discards_the_work_and_removes_everything(self):
        p = self.wt.add(self.root, "grunt1")
        (p / "Plugin.cs").write_text("class X {}\n")

        config.down(self.root, force=True)

        self.assertFalse(bus.team_dir(self.root).exists())
        self.assertNotIn("grunt1", self._registered())

    def test_down_removes_a_clean_worktree_without_force(self):
        self.wt.add(self.root, "grunt1")
        actions = config.down(self.root)
        self.assertIn("removed worktree for grunt1", actions)
        self.assertFalse(bus.team_dir(self.root).exists())
        self.assertNotIn("grunt1", self._registered())

    def test_down_leaves_no_prunable_admin_entry_behind(self):
        self.wt.add(self.root, "grunt1")
        config.down(self.root)
        self.assertNotIn("prunable", self._registered())

    def test_a_plain_directory_under_work_is_not_treated_as_a_worktree(self):
        """`git status` inside a non-worktree dir resolves to the enclosing
        repo and reports the whole main tree. Without the `.git` check, a
        stray directory would make `down` refuse forever."""
        (worktrees.work_dir(self.root) / "leftover").mkdir(parents=True)
        (self.root / "dirty-main-tree.txt").write_text("x\n")
        self.assertEqual(self.wt.agents(self.root), [])
        config.down(self.root)   # must not raise
        self.assertFalse(bus.team_dir(self.root).exists())


class InitPruneTest(_RealRepo):
    def test_init_prunes_after_the_bus_was_removed_by_hand(self):
        self.wt.add(self.root, "grunt1")
        shutil.rmtree(bus.team_dir(self.root))   # `rm -rf .team`, no `down`
        self.assertIn("prunable", self._registered())

        config.init(self.root)

        self.assertNotIn("grunt1", self._registered())
        # And the agent's name is free again.
        self.wt.add(self.root, "grunt1")

    def test_init_reports_but_survives_an_unusable_git(self):
        class Broken:
            def prune(self, root):
                raise worktrees.WorktreeError("git exploded")
            def agents(self, root):
                return []
        lines = config.init(self.root, force=True, wt=Broken())
        self.assertTrue(any("could not prune" in l for l in lines))
        self.assertTrue(bus.team_dir(self.root).is_dir())
