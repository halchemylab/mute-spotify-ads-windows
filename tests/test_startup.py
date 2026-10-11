"""Startup-order regressions without controlling Spotify or Windows audio."""

import asyncio
from datetime import timedelta
import sys
import types
import unittest
from unittest.mock import patch

import mute_spotify_ads as app


class StartupTests(unittest.TestCase):
    def exercise_startup(self, frames, stale_first_manager):
        state = {"frame": 0, "now": 0}
        volume = types.SimpleNamespace(muted=False)
        volume.GetMute = lambda: volume.muted
        volume.SetMute = lambda value, context: setattr(volume, "muted", bool(value))
        audio_session = types.SimpleNamespace(
            Process=types.SimpleNamespace(name=lambda: "Spotify.exe"),
            InstanceIdentifier="spotify-session",
            SimpleAudioVolume=volume,
        )
        muted_frames = []
        requested_at = []
        messages = []

        class MediaSession:
            source_app_user_model_id = "Spotify.exe"

            def get_playback_info(self):
                return types.SimpleNamespace(playback_status="Playing")

            async def try_get_media_properties_async(self):
                ad = frames[state["frame"]] == "ad"
                return types.SimpleNamespace(
                    title="Advertisement" if ad else "Track",
                    artist="" if ad else "Artist",
                    album_title="" if ad else "Album",
                    album_artist="", subtitle="", playback_type=None,
                )

            def get_timeline_properties(self):
                return types.SimpleNamespace(start_time=timedelta(), end_time=timedelta(seconds=30))

        class Manager:
            def __init__(self, stale):
                self.stale = stale

            def get_sessions(self):
                if self.stale or frames[state["frame"]] in {"closed", "idle"}:
                    return []
                return [MediaSession()]

        async def request_manager():
            requested_at.append(state["now"])
            return Manager(stale_first_manager and len(requested_at) == 1)

        class StopLoop(Exception):
            pass

        async def next_frame(_delay):
            muted_frames.append(volume.muted)
            state["frame"] += 1
            state["now"] += 5
            if state["frame"] == len(frames):
                raise StopLoop()

        control = types.ModuleType("winrt.windows.media.control")
        control.GlobalSystemMediaTransportControlsSessionManager = types.SimpleNamespace(request_async=request_manager)
        control.GlobalSystemMediaTransportControlsSessionPlaybackStatus = types.SimpleNamespace(PLAYING="Playing")
        pycaw = types.ModuleType("pycaw.pycaw")
        pycaw.AudioUtilities = types.SimpleNamespace(GetAllSessions=lambda: [audio_session])
        modules = {
            "winrt": types.ModuleType("winrt"),
            "winrt.windows": types.ModuleType("winrt.windows"),
            "winrt.windows.media": types.ModuleType("winrt.windows.media"),
            "winrt.windows.media.control": control,
            "pycaw": types.ModuleType("pycaw"),
            "pycaw.pycaw": pycaw,
            "psutil": types.ModuleType("psutil"),
        }
        with (
            patch.dict(sys.modules, modules),
            patch.object(app, "spotify_running", side_effect=lambda _: frames[state["frame"]] != "closed"),
            patch.object(app.asyncio, "sleep", side_effect=next_frame),
            patch.object(app, "time", types.SimpleNamespace(monotonic=lambda: state["now"])),
            patch.object(app, "add_stats", return_value=5),
            patch.object(app, "log", side_effect=lambda message, level="INFO": messages.append(message)),
            patch.object(app, "TerminalDisplay"),
        ):
            with self.assertRaises(StopLoop):
                asyncio.run(app.run(False, False))

        self.assertTrue(muted_frames[-2], "The newly discovered ad should be muted")
        self.assertFalse(muted_frames[-1], "Music should restore audio")
        self.assertFalse(volume.muted)
        self.assertTrue(any("Music resumed" in message for message in messages))
        return requested_at

    def test_watcher_before_spotify_refreshes_empty_manager_after_15_seconds(self):
        requests = self.exercise_startup(["closed", "idle", "idle", "idle", "ad", "song"], True)
        self.assertEqual(requests, [5, 20])

    def test_spotify_open_before_playback_refreshes_empty_manager_after_15_seconds(self):
        requests = self.exercise_startup(["idle", "idle", "idle", "ad", "song"], True)
        self.assertEqual(requests, [0, 15])

    def test_spotify_already_playing_keeps_connected_manager(self):
        requests = self.exercise_startup(["song", "song", "song", "song", "ad", "song"], False)
        self.assertEqual(requests, [0])

    def test_session_appearing_on_existing_manager_does_not_wait_for_retry(self):
        requests = self.exercise_startup(["idle", "ad", "song"], False)
        self.assertEqual(requests, [0])


if __name__ == "__main__":
    unittest.main()
