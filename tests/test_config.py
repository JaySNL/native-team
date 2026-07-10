import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from team import bus, config


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
        }
        self.assertEqual(config.GRUNT_SETTINGS, expected)

    # -- init: bus tree --

    def test_init_creates_bus_dirs(self):
        config.init(self.root)
        for sub in ("inbox/lead", "results", "staging", "logs", "ids", "dead"):
            self.assertTrue((self.root / ".team" / sub).is_dir(), sub)

    def test_init_writes_roster_json_empty(self):
        config.init(self.root)
        self.assertEqual(bus.read_json(self.root / ".team" / "roster.json"), {})

    def test_init_writes_full_grunt_settings(self):
        config.init(self.root)
        got = json.loads(self.qwen().read_text())
        expected = {
            "context": {"fileName": ["TEAM_GRUNT_CONTEXT.md"]},
            "tools": {
                "approvalMode": "yolo",
                "computerUse": {"enabled": False},
                "excludeTools": ["write_file", "replace", "edit", "save_memory", "web_fetch"],
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
        self.assertEqual(text.count(".team/"), 1)
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


if __name__ == "__main__":
    unittest.main()
