"""Focused checks for polling output without requiring Windows audio devices."""

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

import mute_spotify_ads as app


class FakeVolume:
    def __init__(self):
        self.fail = True
        self.muted = False

    def GetMute(self):
        if self.fail:
            raise RuntimeError("device busy")
        return self.muted

    def SetMute(self, value, _context):
        self.muted = bool(value)


class FakeAudioUtilities:
    def __init__(self, volume):
        self.session = type("Session", (), {
            "Process": type("Process", (), {"name": lambda self: "Spotify.exe"})(),
            "InstanceIdentifier": "spotify-session",
            "SimpleAudioVolume": volume,
        })()

    def GetAllSessions(self):
        return [self.session]


class TerminalOutputTests(unittest.TestCase):
    def test_ad_end_reports_duration_and_total_on_one_line(self):
        volume = FakeVolume()
        volume.fail = False
        audio_utilities = FakeAudioUtilities(volume)

        class ManagerFactory:
            @staticmethod
            async def request_async():
                return object()

        control = types.ModuleType("winrt.windows.media.control")
        control.GlobalSystemMediaTransportControlsSessionManager = ManagerFactory
        control.GlobalSystemMediaTransportControlsSessionPlaybackStatus = types.SimpleNamespace(PLAYING="Playing")
        pycaw = types.ModuleType("pycaw.pycaw")
        pycaw.AudioUtilities = audio_utilities
        modules = {
            "winrt": types.ModuleType("winrt"),
            "winrt.windows": types.ModuleType("winrt.windows"),
            "winrt.windows.media": types.ModuleType("winrt.windows.media"),
            "winrt.windows.media.control": control,
            "pycaw": types.ModuleType("pycaw"),
            "pycaw.pycaw": pycaw,
            "psutil": types.ModuleType("psutil"),
        }
        ad = ("Advertisement", "", "Playing", "", "", "", None, 30)
        song = ("Track", "Artist", "Playing", "Album", "", "", None, 180)
        messages = []

        class StopLoop(Exception):
            pass

        with (
            patch.dict(sys.modules, modules),
            patch.object(app, "spotify_running", return_value=True),
            patch.object(app, "spotify_media", new=AsyncMock(side_effect=[ad, song])),
            patch.object(app.asyncio, "sleep", new=AsyncMock(side_effect=[None, StopLoop()])),
            patch.object(app, "time", new=types.SimpleNamespace(monotonic=iter([100, 127]).__next__)),
            patch.object(app, "add_stats", return_value=763),
            patch.object(app, "log", side_effect=lambda message, level="INFO": messages.append((level, message))),
        ):
            with self.assertRaises(StopLoop):
                asyncio.run(app.run(debug=False, notifications=False))

        summary = [message for _, message in messages if "Music resumed" in message]
        self.assertEqual(summary, ["Music resumed; Spotify audio restored | ad muted: 27s | total: 12m 43s"])
        self.assertFalse(volume.muted)

    def test_mute_failure_is_reported_once_then_recovers(self):
        volume = FakeVolume()
        mute = app.SpotifyMute(FakeAudioUtilities(volume), app.PollErrors())
        messages = []

        with patch.object(app, "log", side_effect=lambda message, level="INFO": messages.append((level, message))):
            mute.mute_current_sessions()
            mute.mute_current_sessions()
            volume.fail = False
            mute.mute_current_sessions()

        self.assertTrue(volume.muted)
        self.assertEqual([level for level, _ in messages], ["WARN", "INFO"])
        self.assertIn("device busy", messages[0][1])
        self.assertIn("recovered", messages[1][1])

    def test_media_read_recovers_after_failing_session_disappears(self):
        class BadSession:
            @property
            def source_app_user_model_id(self):
                raise RuntimeError("metadata unavailable")

        class GoodSession:
            source_app_user_model_id = "Spotify.exe"

            def get_playback_info(self):
                return type("Playback", (), {"playback_status": "Playing"})()

            async def try_get_media_properties_async(self):
                return type("Properties", (), {
                    "title": "Track", "artist": "Artist", "album_title": "Album",
                    "album_artist": "", "subtitle": "", "playback_type": None,
                })()

            def get_timeline_properties(self):
                raise RuntimeError("no timeline")

        class Manager:
            sessions = [BadSession(), GoodSession()]

            def get_sessions(self):
                return self.sessions

        manager = Manager()
        errors = app.PollErrors()
        messages = []
        with patch.object(app, "log", side_effect=lambda message, level="INFO": messages.append((level, message))):
            self.assertEqual(asyncio.run(app.spotify_media(manager, errors))[0], "Track")
            self.assertEqual(asyncio.run(app.spotify_media(manager, errors))[0], "Track")
            self.assertEqual(len(messages), 1)
            manager.sessions = [GoodSession()]
            self.assertEqual(asyncio.run(app.spotify_media(manager, errors))[0], "Track")

        self.assertEqual([level for level, _ in messages], ["WARN", "INFO"])
        self.assertIn("recovered", messages[1][1])


if __name__ == "__main__":
    unittest.main()
