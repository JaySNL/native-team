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


if __name__ == "__main__":
    unittest.main()
