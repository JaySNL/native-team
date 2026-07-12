"""Named buses: several independent teams in ONE git repo, each with its own
`.team-<slug>/` directory, sharing a single `.qwen` by ref count.

The resolver order (flag > $TEAM_BUS > bus-named ancestor > `.team`) is pinned
directly; the multi-bus lifecycle is driven through `cli.main` so the argparse
wiring, the `$TEAM_BUS` handoff, and the config ref-counting are all exercised
together.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from team import bus, cli, config


def _run(root, *args):
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        code = cli.main(["--root", str(root), *args])
    return code, out.getvalue()


class ResolveOrderTest(unittest.TestCase):
    """`resolve_bus_name` precedence, independent of any bus on disk."""

    def test_flag_beats_env_beats_walkup_beats_default(self):
        with tempfile.TemporaryDirectory() as d:
            neutral = Path(d)
            grunt_cwd = neutral / ".team-auth" / "work" / "grunt1"
            grunt_cwd.mkdir(parents=True)

            # (d) default: nothing set, cwd is not inside a bus
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(bus.resolve_bus_name(start=neutral), ".team")
                # (c) walk-up: a grunt's cwd is inside its bus dir
                self.assertEqual(bus.resolve_bus_name(start=grunt_cwd), ".team-auth")

            # (b) env overrides the walk-up
            with mock.patch.dict(os.environ, {"TEAM_BUS": ".team-ui"}, clear=True):
                self.assertEqual(bus.resolve_bus_name(start=grunt_cwd), ".team-ui")
                # (a) the flag overrides the env
                self.assertEqual(
                    bus.resolve_bus_name("auth", start=grunt_cwd), ".team-auth")

    def test_flag_default_and_empty_map_to_plain_team(self):
        with mock.patch.dict(os.environ, {"TEAM_BUS": ".team-x"}, clear=True):
            self.assertEqual(bus.resolve_bus_name("default"), ".team")
            self.assertEqual(bus.resolve_bus_name(""), ".team")

    def test_invalid_slug_and_env_are_rejected(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(bus.BusError):
                bus.resolve_bus_name("bad slug!")
        with mock.patch.dict(os.environ, {"TEAM_BUS": "notabus"}, clear=True):
            with self.assertRaises(bus.BusError):
                bus.resolve_bus_name()

    def test_bus_root_finds_the_right_bus_from_a_grunt_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            grunt_cwd = root / ".team-auth" / "work" / "grunt1"
            grunt_cwd.mkdir(parents=True)
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(bus.bus_root(grunt_cwd), root)


class NamedBusCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / ".git").mkdir()

        # main() writes $TEAM_BUS from a --bus flag; isolate it so it neither
        # leaks in from the runner's shell nor out into sibling tests.
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        os.environ.pop("TEAM_BUS", None)
        self.addCleanup(env.stop)

        # Isolate HOME to an empty dir: init copies the user's global ~/.qwen
        # provider into the project settings, so without this the written file
        # would carry the developer's real provider and never equal the bare
        # grunt_settings() constant these tests pin. (env.stop restores HOME.)
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        os.environ["HOME"] = home.name

        # `resolve_bus_name`'s walk-up starts from cwd; sit in a directory that
        # is not itself inside any bus, so the default really resolves to `.team`.
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(lambda: os.chdir(cwd))

    def qwen(self):
        return self.root / ".qwen" / "settings.json"

    def backup(self):
        return self.root / ".qwen" / "settings.json.team-backup"

    def _reset_env(self):
        # Between lead commands the flag is re-supplied; drop the residue so a
        # step that omits --bus is genuinely testing the unset path.
        os.environ.pop("TEAM_BUS", None)

    # -- two independent buses in one repo --

    def test_two_named_buses_are_independent(self):
        self.qwen().parent.mkdir(parents=True)
        self.qwen().write_text('{"mine": true}')

        self.assertEqual(_run(self.root, "init", "--bus", "auth")[0], 0)
        # first team in: the user's real settings get backed up, exactly once
        self.assertTrue(self.backup().exists())
        self.assertEqual(json.loads(self.backup().read_text()), {"mine": True})

        self._reset_env()
        self.assertEqual(_run(self.root, "init", "--bus", "ui")[0], 0)

        # both buses exist, each with its own inbox / ids / results
        for name in (".team-auth", ".team-ui"):
            for sub in ("inbox/lead", "ids", "results", "staging"):
                self.assertTrue((self.root / name / sub).is_dir(), f"{name}/{sub}")
        # the plain default bus was never created
        self.assertFalse((self.root / ".team").exists())
        # the two id counters are independent files
        self.assertNotEqual((self.root / ".team-auth" / "ids").resolve(),
                            (self.root / ".team-ui" / "ids").resolve())

        # the SECOND init must not clobber the backup the first one made
        self.assertEqual(json.loads(self.backup().read_text()), {"mine": True})
        # and the shared settings.json is our grunt config while a bus is live
        self.assertEqual(json.loads(self.qwen().read_text()), config.grunt_settings())

    def test_backup_made_once_and_not_when_no_prior_settings(self):
        # no user settings.json: first init creates ours fresh, no backup file
        _run(self.root, "init", "--bus", "auth")
        self.assertFalse(self.backup().exists())
        self.assertEqual(json.loads(self.qwen().read_text()), config.grunt_settings())
        self._reset_env()
        _run(self.root, "init", "--bus", "ui")
        self.assertFalse(self.backup().exists())

    # -- backward compatibility: no flag, no env => exactly `.team` --

    def test_default_still_creates_plain_team(self):
        self.assertEqual(_run(self.root, "init")[0], 0)
        self.assertTrue((self.root / ".team" / "ids").is_dir())
        self.assertFalse((self.root / ".team-auth").exists())
        # and no named-bus adoption hint is printed
        self.assertNotIn("export TEAM_BUS", _run(self.root, "init", "--force")[1])

    def test_named_init_prints_copy_pasteable_export_hint(self):
        _, out = _run(self.root, "init", "--bus", "auth")
        self.assertIn("export TEAM_BUS=.team-auth", out)

    # -- down leaves the project .qwen (project-owned, persists) --

    def test_down_leaves_qwen_and_backup_for_every_bus(self):
        self.qwen().parent.mkdir(parents=True)
        self.qwen().write_text('{"mine": true}')
        _run(self.root, "init", "--bus", "auth")     # snapshots {"mine"} -> backup
        self._reset_env()
        _run(self.root, "init", "--bus", "ui")

        # down one bus while the other is still live: .qwen and backup left alone
        self._reset_env()
        self.assertEqual(_run(self.root, "down", "--bus", "auth")[0], 0)
        self.assertFalse((self.root / ".team-auth").exists())
        self.assertTrue((self.root / ".team-ui").exists())
        self.assertTrue(self.backup().exists())
        self.assertEqual(json.loads(self.qwen().read_text()), config.grunt_settings())

        # down the last bus: project .qwen still the team's; original still in backup
        self._reset_env()
        self.assertEqual(_run(self.root, "down", "--bus", "ui")[0], 0)
        self.assertFalse((self.root / ".team-ui").exists())
        self.assertEqual(json.loads(self.qwen().read_text()), config.grunt_settings())
        self.assertEqual(json.loads(self.backup().read_text()), {"mine": True})

    def test_down_leaves_fresh_settings_in_place(self):
        # no user settings; two buses share our fresh grunt config
        _run(self.root, "init", "--bus", "auth")
        self._reset_env()
        _run(self.root, "init", "--bus", "ui")

        self._reset_env()
        _run(self.root, "down", "--bus", "auth")     # not the last bus
        self.assertTrue(self.qwen().exists())

        self._reset_env()
        _run(self.root, "down", "--bus", "ui")        # the last bus out
        self.assertTrue(self.qwen().exists())         # project-owned, persists
        self.assertEqual(json.loads(self.qwen().read_text()), config.grunt_settings())


if __name__ == "__main__":
    unittest.main()
