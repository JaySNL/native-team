import unittest

from team import log


class LogTest(unittest.TestCase):
    def test_strips_ansi_sgr_and_cursor_codes(self):
        self.assertEqual(log.render("\x1b[32mhello\x1b[0m\x1b[2K"), "hello")

    def test_strips_osc_title_sequences(self):
        self.assertEqual(log.render("\x1b]0;title\x07kept"), "kept")

    def test_drops_spinner_frames(self):
        raw = ("... I'll be back... with an answer. (7.5s · esc to cancel)\n"
               "◆ PINEAPPLE\n")
        self.assertEqual(log.render(raw), "◆ PINEAPPLE")

    def test_dedupes_repeated_redraw_lines(self):
        raw = "> prompt\n> prompt\n> prompt\nanswer\n"
        self.assertEqual(log.render(raw), "> prompt\nanswer")

    def test_carriage_returns_become_line_breaks(self):
        self.assertEqual(log.render("a\rb\r"), "a\nb")

    def test_blank_lines_dropped(self):
        self.assertEqual(log.render("a\n\n   \nb\n"), "a\nb")

    def test_preserves_first_occurrence_order(self):
        self.assertEqual(log.render("b\na\nb\n"), "b\na")

    def test_truncated_escape_at_eof_incomplete_csi(self):
        """Truncated CSI sequence at EOF must not leak into output."""
        result = log.render("real line\n\x1b[3")
        self.assertIn("real line", result)
        self.assertNotIn("\x1b", result)

    def test_truncated_escape_at_eof_incomplete_osc(self):
        """Truncated OSC sequence at EOF must not leak into output."""
        result = log.render("real line\n\x1b]0;tit")
        self.assertIn("real line", result)
        self.assertNotIn("\x1b", result)

    def test_truncated_escape_at_eof_bare_esc(self):
        """Bare ESC at EOF must not leak into output."""
        result = log.render("real line\n\x1b")
        self.assertIn("real line", result)
        self.assertNotIn("\x1b", result)

    def test_osc_terminated_by_string_terminator(self):
        """OSC sequences can be terminated by ST (ESC-backslash) as well as BEL."""
        # ST is \x1b\\ (ESC followed by one backslash)
        result = log.render("\x1b]0;title\x1b\\kept")
        self.assertEqual(result, "kept")

    def test_osc_terminated_by_bel_still_works(self):
        """Regression: BEL-terminated OSC must still work."""
        result = log.render("\x1b]0;title\x07kept")
        self.assertEqual(result, "kept")

    def test_no_escape_bytes_in_output(self):
        """Property test: output must contain zero \\x1b and zero \\x07 bytes."""
        # Synthetic input with SGR, OSC, spinner, duplicates, and mixed terminators
        raw = (
            "\x1b[32mcolored\x1b[0m\n"           # SGR
            "colored\n"                            # duplicate
            "\x1b]0;title\x07"                    # OSC with BEL
            "kept\n"                               # content
            "kept\n"                               # duplicate
            "line (1.5s · esc to cancel)\n"       # spinner
            "\x1b]0;another\x1b\\osc_st\n"        # OSC with ST
            "osc_st\n"                             # content
        )
        result = log.render(raw)
        self.assertNotIn("\x1b", result, "Output must not contain ESC byte")
        self.assertNotIn("\x07", result, "Output must not contain BEL byte")
        # Verify we got expected content
        self.assertIn("colored", result)
        self.assertIn("kept", result)
        self.assertIn("osc_st", result)

    def test_osc_must_not_cross_newlines_defect_1(self):
        """Finding 1: OSC pattern must not swallow real content lines across newlines.

        A truncated or unterminated OSC sequence should not match greedily
        across any number of subsequent lines until it finds a later \\x07 or \\x1b\\\\.
        Both 'real line 1' and 'real line 2' must be preserved.
        """
        result = log.render("\x1b]0;title\nreal line 1\nreal line 2\x07after")
        self.assertIn("real line 1", result, "First real line should be preserved")
        self.assertIn("real line 2", result, "Second real line should be preserved")
        self.assertIn("after", result, "Content after OSC terminator should be preserved")

    def test_torn_line_collision_defect_2(self):
        """Finding 2: A line with a residual escape after truncation can collide
        with a real distinct line via global dedupe and shadow it.

        The genuine third line 'AAA' should not be lost just because a torn
        line was previously truncated to 'AAA'.
        """
        # First 'AAA' is clean; second 'AAA\x1b[9' has a truncated escape;
        # third 'AAA' is real again. With truncation-and-truncate strategy,
        # the torn line becomes 'AAA', which then dedupes the real third 'AAA'.
        result = log.render("AAA\n\x1b[9\nBBB\nAAA\n")
        lines = result.split("\n")
        # Should contain both 'AAA' and 'BBB', with the real 'AAA' present
        self.assertIn("AAA", lines, f"AAA should be in output lines: {lines}")
        self.assertIn("BBB", lines, f"BBB should be in output lines: {lines}")
        # Verify the exact lines
        self.assertEqual(set(lines), {"AAA", "BBB"}, f"Expected exactly AAA and BBB, got {lines}")

    def test_8bit_c1_csi_not_in_output(self):
        """The 8-bit C1 CSI introducer \\x9b is not the same as \\x1b[.
        Both must be stripped from output.
        """
        result = log.render("before\x9b[0mafter")
        self.assertNotIn("\x9b", result, "8-bit CSI introducer must not be in output")
        # The content may be corrupted by the unrecognized sequence, but no escape should remain
        self.assertNotIn("\x1b", result, "ESC must not be in output")

    def test_both_osc_terminators_still_work_regression(self):
        """Regression: Ensure both OSC terminators (BEL and ST) continue to work."""
        result_bel = log.render("\x1b]0;title\x07kept")
        self.assertEqual(result_bel, "kept", "BEL-terminated OSC must work")

        result_st = log.render("\x1b]0;title\x1b\\kept")
        self.assertEqual(result_st, "kept", "ST-terminated OSC must work")

    def test_dcs_sequence_produces_no_escapes(self):
        """DCS-style sequences (\\x1bP...\\x1b\\\\) must also not leave escapes in output."""
        result = log.render("\x1bPsomething\x1b\\tail")
        self.assertNotIn("\x1b", result, "DCS sequence must not leak escapes")
        self.assertNotIn("\x9b", result, "8-bit CSI must not be in output")


if __name__ == "__main__":
    unittest.main()
