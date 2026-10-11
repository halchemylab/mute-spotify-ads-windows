"""Allowlist behavior without Windows audio devices."""

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

import mute_spotify_ads as app


class AllowlistTests(unittest.TestCase):
    def test_exact_case_insensitive_matching(self):
        titles = {"short track"}
        artists = {"my podcast"}
        self.assertTrue(app.is_allowed(" Short Track ", "Other", titles, artists))
        self.assertTrue(app.is_allowed("Episode 1", "MY PODCAST", titles, artists))
        self.assertFalse(app.is_allowed("Short Track Remix", "Other", titles, artists))
        self.assertFalse(app.is_allowed("Advertisement", "", titles, artists))

    def test_command_line_options_reach_watcher(self):
        with (
            patch.object(app.sys, "argv", ["mute_spotify_ads.py", "--allow-title", " Short Track ",
                                            "--allow-artist", "My Podcast"]),
            patch.object(app.sys, "platform", "win32"),
            patch.object(app, "SingleInstance"),
            patch.object(app, "run", new_callable=AsyncMock) as watcher,
        ):
            self.assertEqual(app.main(), 0)

        watcher.assert_awaited_once_with(False, False, {"short track"}, {"my podcast"})

    def test_allowed_item_after_ad_restores_audio(self):
        class Volume:
            muted = False

            def GetMute(self):
                return self.muted

            def SetMute(self, value, _context):
                self.muted = bool(value)

        volume = Volume()
        session = types.SimpleNamespace(
            Process=types.SimpleNamespace(name=lambda: "Spotify.exe"),
            InstanceIdentifier="spotify-session",
            SimpleAudioVolume=volume,
        )
        audio_utilities = types.SimpleNamespace(GetAllSessions=lambda: [session])

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
        allowed = ("Episode 1", "My Podcast", "Playing", "", "", "", None, 30)
        messages = []

        class StopLoop(Exception):
            pass

        with (
            patch.dict(sys.modules, modules),
            patch.object(app, "spotify_running", return_value=True),
            patch.object(app, "spotify_media", new=AsyncMock(side_effect=[ad, allowed])),
            patch.object(app.asyncio, "sleep", new=AsyncMock(side_effect=[None, StopLoop()])),
            patch.object(app, "add_stats", return_value=30),
            patch.object(app, "log", side_effect=lambda message, level="INFO": messages.append(message)),
        ):
            with self.assertRaises(StopLoop):
                asyncio.run(app.run(False, False, allowed_artists={"my podcast"}))

        self.assertFalse(volume.muted)
        self.assertTrue(any("Allowed item started" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
