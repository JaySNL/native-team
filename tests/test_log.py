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
        """Finding 2: a torn line must not shadow a genuine later line.

        The escape sits on the SAME line as the first 'AAA'. Truncating that
        line yields the fragment 'AAA', which the global dedupe then uses to
        delete the real third-line 'AAA'. Dropping the torn line instead
        leaves the genuine 'AAA' intact.
        """
        result = log.render("AAA\x1b[9\nBBB\nAAA\n")
        # Torn line dropped; 'BBB' and the genuine 'AAA' survive, in order.
        self.assertEqual(result.split("\n"), ["BBB", "AAA"])

    def test_8bit_c1_csi_is_stripped_and_content_survives(self):
        """A genuine 8-bit CSI (\\x9b, the C1 twin of \\x1b[) is stripped in place,
        so the text it wraps survives.

        Note the sequence has no '[' -- \\x9b IS the introducer. Writing
        "\\x9b[0m" would be a malformed sequence the pattern must not match,
        which is why that input proves nothing about this alternative.
        """
        self.assertEqual(log.render("before\x9b0mafter"), "beforeafter")

    def test_malformed_8bit_csi_drops_the_line(self):
        """A \\x9b that starts no valid sequence is a torn write: the line goes."""
        self.assertEqual(log.render("keep\nbefore\x9b[0mafter"), "keep")

    def test_charset_selection_is_stripped_and_content_survives(self):
        """Charset switches are zero-width and sit next to real text.

        The 341 KB qwen capture contains none -- Ink draws borders with Unicode,
        not the alt charset. But the grunt's shell is unrestricted, and `tput
        sgr0`, ncurses, and git colour output all emit "\\x1b(B". Without this
        alternative the residual ESC drops the whole line, taking real
        transcript with it.
        """
        self.assertEqual(log.render("before\x1b(Bafter"), "beforeafter")
        self.assertEqual(log.render("box\x1b(0qqq\x1b(Bend"), "boxqqqend")

    def test_keypad_mode_is_stripped_and_content_survives(self):
        """Keypad mode switches are zero-width; same reasoning as charset."""
        self.assertEqual(log.render("before\x1b=after"), "beforeafter")
        self.assertEqual(log.render("before\x1b>after"), "beforeafter")

    def test_both_osc_terminators_still_work_regression(self):
        """Regression: Ensure both OSC terminators (BEL and ST) continue to work."""
        result_bel = log.render("\x1b]0;title\x07kept")
        self.assertEqual(result_bel, "kept", "BEL-terminated OSC must work")

        result_st = log.render("\x1b]0;title\x1b\\kept")
        self.assertEqual(result_st, "kept", "ST-terminated OSC must work")

    def test_dcs_line_is_dropped_not_parsed(self):
        """There is deliberately no DCS alternative in the pattern.

        A DCS body is a non-printing device-control payload, so the drop-line
        safety net loses nothing real. qwen emits none (measured: zero \\x1bP
        in a 341 KB capture). The guarantee that matters is that no escape
        byte reaches the output.
        """
        self.assertEqual(log.render("keep\n\x1bPsomething\x1b\\tail"), "keep")


if __name__ == "__main__":
    unittest.main()


class SpinnerFormatTest(unittest.TestCase):
    """qwen formats elapsed time four ways. A seconds-only filter left a real
    1.1 MB capture full of spinner frames.
    """

    def test_every_elapsed_format_is_dropped(self):
        for elapsed in ("7.5s", "0.1s", "2m", "2m 15s", "1h 2m 3s"):
            with self.subTest(elapsed=elapsed):
                raw = f"keep me\n({elapsed} · esc to cancel)\n"
                self.assertEqual(log.render(raw), "keep me")

    def test_spinner_with_a_token_counter_is_dropped(self):
        """qwen appends "· ↑ 316 tokens" between the elapsed time and
        "esc to cancel" once it starts streaming. Observed live.
        """
        raw = "keep me\n(49.7s · \u2191 316 tokens · esc to cancel)\n"
        self.assertEqual(log.render(raw), "keep me")

    def test_spinner_wrapped_across_two_lines_is_dropped(self):
        """In a narrow pane qwen wraps the spinner. Neither half matches the
        whole-frame pattern. Observed live: 4 frames survived a 1.1 MB capture.
        """
        raw = ("keep me\n"
               ".. joke text (2m 1s \u00b7 \u2191 740 tokens \u00b7\n"
               "  .                  esc to cancel)\n"
               "keep me too\n")
        self.assertEqual(log.render(raw), "keep me\nkeep me too")

    def test_a_real_line_that_merely_mentions_cancelling_survives(self):
        self.assertEqual(log.render("(3 items · esc to cancel)\n"),
                         "(3 items · esc to cancel)")
