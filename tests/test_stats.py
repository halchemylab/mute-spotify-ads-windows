"""Stats history and summary checks that do not need Windows audio devices."""

from contextlib import redirect_stdout
from datetime import datetime, time, timedelta
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mute_spotify_ads as app


class StatsTests(unittest.TestCase):
    def test_legacy_total_is_preserved_when_new_ad_is_appended(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stats.txt"
            path.write_text("600", encoding="utf-8")
            with patch.object(app, "STATS_PATH", path):
                self.assertEqual(app.add_stats(42), 642)
                self.assertEqual(app.read_stats(), 642)
                legacy, entries, has_legacy = app.read_stats_history()

            self.assertEqual(legacy, 600)
            self.assertTrue(has_legacy)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0][1], 42)
            self.assertIsNotNone(entries[0][0].tzinfo)
            self.assertEqual(path.read_text(encoding="utf-8").splitlines()[0], "600")

    def test_summary_groups_recent_ads_and_labels_unknown_legacy_count(self):
        now = datetime.now().astimezone()
        today = now.date()
        week_start = today - timedelta(days=today.weekday())
        prior_week = datetime.combine(week_start - timedelta(days=1), time(12, tzinfo=now.tzinfo))
        entries = [
            (now, 42),
            (now, 60),
            (prior_week, 180),
        ]
        output = StringIO()
        with patch.object(app, "read_stats_history", return_value=(300, entries, True)), redirect_stdout(output):
            app.show_stats()

        lines = output.getvalue().splitlines()
        self.assertIn("2 ads muted · 1m 42s", lines[0])
        self.assertIn("2 ads muted · 1m 42s", lines[1])
        self.assertIn("3 recorded ads · 9m 42s", lines[2])
        self.assertIn("previous stats file", lines[3])

    def test_stats_flag_works_without_windows(self):
        with (
            patch.object(app.sys, "argv", ["mute_spotify_ads.py", "--stats"]),
            patch.object(app.sys, "platform", "linux"),
            patch.object(app, "read_stats_history", return_value=(0, [], False)),
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(app.main(), 0)
        self.assertIn("Today:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
