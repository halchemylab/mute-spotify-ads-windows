"""Terminal rendering checks independent of Windows media sessions."""

from io import StringIO
import unittest
from unittest.mock import patch

from terminal_display import TerminalDisplay


class DisplayTests(unittest.TestCase):
    def test_plain_output_has_transitions_and_no_ansi(self):
        stream = StringIO()
        display = TerminalDisplay(stream)
        display.event("Spotify connected")
        display.update("Monitoring music", "Song — Artist")
        display.update("Monitoring music", "Song — Artist")
        display.update("Paused", "Song — Artist")
        output = stream.getvalue()
        self.assertIn("Spotify connected", output)
        self.assertEqual(output.count("Monitoring music"), 1)
        self.assertIn("Paused | Song - Artist", output)
        self.assertNotIn("\x1b[", output)

    def test_interactive_redraws_only_for_changes_and_bounds_history(self):
        stream = StringIO()
        with patch.object(TerminalDisplay, "_enable_interactive", return_value=True):
            display = TerminalDisplay(stream)
        display.color = False
        with patch.object(display, "_fits", return_value=True):
            display.update("Waiting for Spotify")
            first = stream.getvalue()
            display.update("Waiting for Spotify")
            self.assertEqual(stream.getvalue(), first)
            for index in range(7):
                display.event(f"Event {index}")
            display.update("Ad muted")
        output = stream.getvalue()
        self.assertIn("┌", output)
        self.assertIn("SPOTIFY / AD MUTER", output)
        self.assertIn("●  AD MUTED", output)
        self.assertIn("\x1b[", output)
        self.assertNotIn("\x1b[36m", output)
        self.assertNotIn("Event 0", output.split("\x1b[2K┌")[-1])
        self.assertIn("Event 6", output)
        self.assertEqual(len(display.events), 5)

    def test_metadata_cannot_inject_terminal_controls(self):
        stream = StringIO()
        display = TerminalDisplay(stream)
        display.update("Monitoring music", "Song\x1b[2J\nArtist")
        self.assertIn("Song [2J Artist", stream.getvalue())
        self.assertNotIn("\x1b", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
