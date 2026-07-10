import subprocess
import unittest
from pathlib import Path
from unittest import mock

from team import panes


class FakeRunner:
    """Records argv; replays queued (returncode, stdout) pairs."""

    def __init__(self, replies=None):
        self.calls: list[list[str]] = []
        self.replies = list(replies or [])

    def __call__(self, argv):
        self.calls.append(argv)
        rc, out = self.replies.pop(0) if self.replies else (0, "")
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


class PanesTest(unittest.TestCase):
    def mk(self, replies=None):
        runner = FakeRunner(replies)
        return runner, panes.Panes(runner=runner, sleep=lambda _s: None)

    # --- send_line: exact argv, exact order ---

    def test_send_line_sends_escape_then_literal_then_enter(self):
        runner, p = self.mk()
        p.send_line("team:0.1", "do task .team/inbox/grunt1/001.json")
        self.assertEqual(
            runner.calls,
            [
                ["tmux", "send-keys", "-t", "team:0.1", "Escape"],
                ["tmux", "send-keys", "-t", "team:0.1", "-l",
                 "do task .team/inbox/grunt1/001.json"],
                ["tmux", "send-keys", "-t", "team:0.1", "Enter"],
            ],
        )

    def test_escape_precedes_literal_on_every_send(self):
        """Regression guard: a refactor that drops or reorders the leading
        Escape on any send must fail this test. Escape is not cosmetic --
        it both dismisses the command palette and cancels an in-flight qwen
        turn (see module docstring); losing it on any call is a real defect.
        """
        runner, p = self.mk()
        p.send_line("team:0.1", "first")
        p.send_line("team:0.1", "second")
        self.assertEqual(
            runner.calls,
            [
                ["tmux", "send-keys", "-t", "team:0.1", "Escape"],
                ["tmux", "send-keys", "-t", "team:0.1", "-l", "first"],
                ["tmux", "send-keys", "-t", "team:0.1", "Enter"],
                ["tmux", "send-keys", "-t", "team:0.1", "Escape"],
                ["tmux", "send-keys", "-t", "team:0.1", "-l", "second"],
                ["tmux", "send-keys", "-t", "team:0.1", "Enter"],
            ],
        )

    def test_send_line_never_shell_interpolates(self):
        runner, p = self.mk()
        text = 'weird; rm -rf / "$(x)"'
        p.send_line("team:0.1", text)
        self.assertEqual(
            runner.calls[1],
            ["tmux", "send-keys", "-t", "team:0.1", "-l", text],
        )

    # --- exists ---

    def test_exists_false_when_has_session_fails(self):
        runner, p = self.mk(replies=[(1, "")])
        self.assertFalse(p.exists("team:0.1"))
        self.assertEqual(runner.calls, [["tmux", "has-session", "-t", "team"]])

    def test_exists_true_when_pane_listed(self):
        runner, p = self.mk(replies=[(0, ""), (0, "%3\n")])
        self.assertTrue(p.exists("team:0.1"))
        self.assertEqual(
            runner.calls,
            [
                ["tmux", "has-session", "-t", "team"],
                ["tmux", "list-panes", "-t", "team:0.1", "-F", "#{pane_id}"],
            ],
        )

    def test_exists_false_when_pane_list_empty(self):
        runner, p = self.mk(replies=[(0, ""), (0, "")])
        self.assertFalse(p.exists("team:0.1"))

    # --- capture ---

    def test_capture_returns_pane_text_on_success(self):
        runner, p = self.mk(replies=[(0, "hello world\n")])
        self.assertEqual(p.capture("team:0.1"), "hello world\n")
        self.assertEqual(
            runner.calls, [["tmux", "capture-pane", "-p", "-t", "team:0.1"]]
        )

    def test_capture_raises_on_tmux_failure(self):
        runner, p = self.mk(replies=[(1, "")])
        with self.assertRaises(panes.PaneError):
            p.capture("team:0.1")

    def test_capture_error_includes_stderr(self):
        runner = FakeRunner()

        def failing(argv):
            runner.calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such pane")

        p = panes.Panes(runner=failing, sleep=lambda _s: None)
        with self.assertRaisesRegex(panes.PaneError, "no such pane"):
            p.capture("team:0.1")

    # --- clear_context ---

    def test_clear_context_succeeds_once_palette_closes(self):
        # 3 send-keys calls, then capture shows palette, then capture is clean
        runner, p = self.mk(
            replies=[
                (0, ""), (0, ""), (0, ""),
                (0, "  (1/70)\n> clear"),
                (0, "> ready"),
            ]
        )
        p.clear_context("team:0.1", timeout=5.0)
        self.assertEqual(
            runner.calls,
            [
                ["tmux", "send-keys", "-t", "team:0.1", "Escape"],
                ["tmux", "send-keys", "-t", "team:0.1", "-l", "/clear"],
                ["tmux", "send-keys", "-t", "team:0.1", "Enter"],
                ["tmux", "capture-pane", "-p", "-t", "team:0.1"],
                ["tmux", "capture-pane", "-p", "-t", "team:0.1"],
            ],
        )

    def test_clear_context_raises_if_palette_never_closes(self):
        replies = [(0, ""), (0, ""), (0, "")] + [(0, "  (1/70)")] * 200
        runner, p = self.mk(replies=replies)
        with self.assertRaisesRegex(panes.PaneError, "team:0.1"):
            p.clear_context("team:0.1", timeout=0.0)

    # --- pipe_pane ---

    def test_pipe_pane_targets_logfile(self):
        runner, p = self.mk()
        p.pipe_pane("team:0.1", Path("/tmp/x.log"))
        self.assertEqual(
            runner.calls,
            [["tmux", "pipe-pane", "-o", "-t", "team:0.1", "cat >> /tmp/x.log"]],
        )

    def test_pipe_pane_raises_on_tmux_failure(self):
        runner, p = self.mk(replies=[(1, "")])
        with self.assertRaises(panes.PaneError):
            p.pipe_pane("team:0.1", Path("/tmp/x.log"))


class DefaultRunnerTest(unittest.TestCase):
    def test_default_runner_calls_subprocess_without_shell(self):
        with mock.patch("team.panes.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["tmux"], 0, stdout="", stderr=""
            )
            panes.default_runner(["tmux", "has-session", "-t", "team"])
            mock_run.assert_called_once_with(
                ["tmux", "has-session", "-t", "team"],
                capture_output=True,
                text=True,
            )


class PipeQuotingTest(unittest.TestCase):
    """tmux hands pipe-pane's final argument to `sh -c`. It is the only
    shell-interpreted string this module builds, so it must stay quoted.
    A path with no metacharacters cannot prove that -- use one that has them.
    """

    def test_pipe_pane_quotes_a_logfile_path_with_a_space(self):
        runner = FakeRunner()
        panes.Panes(runner=runner, sleep=lambda _: None).pipe_pane(
            "%1", Path("/tmp/x y.log")
        )
        self.assertEqual(
            runner.calls,
            [["tmux", "pipe-pane", "-o", "-t", "%1", "cat >> '/tmp/x y.log'"]],
        )

    def test_pipe_pane_neutralizes_shell_metacharacters(self):
        runner = FakeRunner()
        panes.Panes(runner=runner, sleep=lambda _: None).pipe_pane(
            "%1", Path("/tmp/a;touch /tmp/PWNED")
        )
        self.assertEqual(
            runner.calls,
            [["tmux", "pipe-pane", "-o", "-t", "%1", "cat >> '/tmp/a;touch /tmp/PWNED'"]],
        )


class PalettePostconditionTest(unittest.TestCase):
    def test_palette_regex_ignores_ordinary_parentheses(self):
        """The postcondition looks for the palette's "(3/70)" counter.

        Loosened to a bare "(", it would read any parenthesis in ordinary
        output as "palette still open" and spin until timeout.
        """
        self.assertIsNone(panes.PALETTE.search("> done (see notes) and (x)"))

    def test_palette_regex_fires_on_the_counter(self):
        self.assertTrue(panes.PALETTE.search("  (3/70) /clear"))

    def test_clear_context_accepts_a_clean_pane_on_the_first_capture(self):
        """A pane whose output merely contains parentheses is already clean,
        so clear_context must not capture a second time.
        """
        runner = FakeRunner(replies=[(0, ""), (0, ""), (0, ""), (0, "> done (see notes)")])
        panes.Panes(runner=runner, sleep=lambda _: None).clear_context("%1")
        captures = [c for c in runner.calls if "capture-pane" in c]
        self.assertEqual(len(captures), 1, runner.calls)


class SendPacingTest(unittest.TestCase):
    def test_send_line_sleeps_between_the_separate_key_calls(self):
        """qwen's Ink TUI drops keys sent back-to-back; the probe used a
        delay between Escape, the literal text, and Enter. Pin that a delay
        happens -- not its exact duration.
        """
        naps = []
        panes.Panes(runner=FakeRunner(), sleep=naps.append).send_line("%1", "hi")
        self.assertEqual(len(naps), 2)
        self.assertTrue(all(n > 0 for n in naps), naps)


class TmuxMissingTest(unittest.TestCase):
    def test_default_runner_raises_pane_error_when_tmux_is_absent(self):
        with mock.patch("team.panes.subprocess.run", side_effect=FileNotFoundError("tmux")):
            with self.assertRaisesRegex(panes.PaneError, "tmux not found on PATH"):
                panes.default_runner(["tmux", "list-panes"])


if __name__ == "__main__":
    unittest.main()
