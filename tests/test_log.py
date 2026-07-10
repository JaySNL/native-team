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


if __name__ == "__main__":
    unittest.main()
