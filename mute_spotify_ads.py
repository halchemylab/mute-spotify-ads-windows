"""Mute Spotify's Windows audio sessions while its SMTC metadata indicates an ad."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import os
from pathlib import Path
import sys
import time

if sys.platform == "win32":
    # pycaw/comtypes and WinRT are used on the same asyncio thread.
    sys.coinit_flags = 0  # COINIT_MULTITHREADED


POLL_INTERVAL_SECONDS = 0.5
MAX_AD_DURATION_SECONDS = 60
STATS_PATH = Path(os.environ.get("MUTE_SPOTIFY_STATS_PATH") or Path(__file__).resolve().parent / "stats.txt")
MUTEX_NAME = "Local\\MuteSpotifyAdsWindows"

def is_ad(title: str, artist: str, album: str, duration_seconds: float | None) -> bool:
    """Match common ad labels or short Spotify items without an album."""
    title = title.strip().casefold()
    artist = artist.strip().casefold()
    if title in {"advertisement", "spotify"} and artist in {"", "spotify"}:
        return True
    return (
        not album.strip()
        and duration_seconds is not None
        and 0 < duration_seconds < MAX_AD_DURATION_SECONDS
    )


class SingleInstance:
    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        self._kernel32 = kernel32
        self._handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "Could not create instance mutex")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            self.close()
            raise RuntimeError("Mute Spotify Ads is already running in this Windows session.")

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def spotify_running(psutil_module) -> bool:
    for process in psutil_module.process_iter(["name"]):
        try:
            if (process.info["name"] or "").casefold() == "spotify.exe":
                return True
        except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
            continue
    return False


def spotify_audio_sessions(audio_utilities):
    """Return all current Spotify sessions, including sessions from child processes."""
    result = []
    for session in audio_utilities.GetAllSessions():
        try:
            process = session.Process
            if process and process.name().casefold() == "spotify.exe":
                result.append(session)
        except Exception as exc:
            print(f"Could not inspect an audio session: {exc}", flush=True)
    return result


class SpotifyMute:
    def __init__(self, audio_utilities) -> None:
        self.audio_utilities = audio_utilities
        # Only entries that this script changed from unmuted to muted are owned.
        self.owned: dict[str, object] = {}

    def mute_current_sessions(self) -> None:
        try:
            sessions = spotify_audio_sessions(self.audio_utilities)
        except Exception as exc:
            print(f"Could not list audio sessions: {exc}", flush=True)
            return
        for session in sessions:
            try:
                key = session.InstanceIdentifier
                volume = session.SimpleAudioVolume
                if key in self.owned:
                    # A user or Spotify can change mute state during an ad.
                    if not volume.GetMute():
                        volume.SetMute(1, None)
                    self.owned[key] = volume
                elif not volume.GetMute():
                    volume.SetMute(1, None)
                    self.owned[key] = volume
            except Exception as exc:
                print(f"Could not mute a Spotify audio session: {exc}", flush=True)

    def restore(self) -> None:
        for key, volume in list(self.owned.items()):
            try:
                volume.SetMute(0, None)
            except Exception as exc:
                # The owning Spotify process may already have exited.
                print(f"Could not restore a closed Spotify audio session: {exc}", flush=True)
            finally:
                del self.owned[key]


def read_stats() -> int:
    try:
        return max(0, int(STATS_PATH.read_text(encoding="utf-8").strip()))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as exc:
        print(f"Could not read stats from {STATS_PATH}: {exc}; starting at zero.", flush=True)
        return 0


def add_stats(seconds: int) -> int:
    total = read_stats() + seconds
    try:
        STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATS_PATH.write_text(f"{total}\n", encoding="utf-8")
    except OSError as exc:
        print(f"Could not write stats to {STATS_PATH}: {exc}", flush=True)
    return total


def total_message(seconds: int) -> str:
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes} mins, {remainder} secs of ads silenced so far"


def notify(enabled: bool, title: str, message: str) -> None:
    if not enabled:
        return
    try:
        from winotify import Notification

        Notification(app_id="Mute Spotify Ads", title=title, msg=message).show()
    except Exception as exc:
        print(f"Could not show notification: {exc}", flush=True)


async def spotify_media(manager):
    """Return Spotify media details, or None if no SMTC session exists."""
    for session in manager.get_sessions():
        try:
            if "spotify" not in session.source_app_user_model_id.casefold():
                continue
            playback = session.get_playback_info().playback_status
            properties = await asyncio.wait_for(session.try_get_media_properties_async(), timeout=3)
            title = (properties.title or "") if properties else ""
            artist = (properties.artist or "") if properties else ""
            album = (properties.album_title or "") if properties else ""
            album_artist = (properties.album_artist or "") if properties else ""
            subtitle = (properties.subtitle or "") if properties else ""
            # pywinrt resolves this enum through an optional Windows.Media module.
            # A missing module must not hide the essential title/artist/status.
            try:
                playback_type = properties.playback_type if properties else None
            except (AttributeError, ImportError):
                playback_type = None
            try:
                timeline = session.get_timeline_properties()
                duration = (timeline.end_time - timeline.start_time).total_seconds()
            except Exception:
                duration = None
            return title, artist, playback, album, album_artist, subtitle, playback_type, duration
        except Exception as exc:
            print(f"Could not read Spotify media session: {exc}", flush=True)
    return None


async def run(debug: bool, notifications: bool) -> None:
    from winrt.windows.media import control as media_control
    from pycaw.pycaw import AudioUtilities
    import psutil

    mute = SpotifyMute(AudioUtilities)
    ad_started: float | None = None
    unseen = object()
    last_debug = unseen
    spotify_was_running: bool | None = None
    manager = None
    last_media_error = None

    def finish_ad(reason: str) -> None:
        nonlocal ad_started
        if ad_started is None:
            return
        mute.restore()
        elapsed = max(0, round(time.monotonic() - ad_started))
        ad_started = None
        print(reason, flush=True)
        total = add_stats(elapsed)
        message = total_message(total)
        print(message, flush=True)
        if reason.startswith("Music resumed"):
            notify(notifications, "Spotify music resumed", message)

    print("Watching Spotify desktop ads. Press Ctrl+C to stop.", flush=True)
    try:
        while True:
            try:
                running = spotify_running(psutil)
            except Exception as exc:
                print(f"Could not check whether Spotify is running: {exc}", flush=True)
                running = True  # Do not change mute state on an uncertain result.

            if not running:
                finish_ad("Spotify closed; its audio sessions were restored.")
                if spotify_was_running is not False:
                    print("Spotify is not running; waiting for it to start.", flush=True)
                spotify_was_running = False
                manager = None
                last_debug = unseen
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            spotify_was_running = True
            try:
                if manager is None:
                    manager = await asyncio.wait_for(
                        media_control.GlobalSystemMediaTransportControlsSessionManager.request_async(),
                        timeout=3,
                    )
                metadata = await spotify_media(manager)
                last_media_error = None
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                if error != last_media_error:
                    print(f"Could not query Windows media sessions: {error}", flush=True)
                last_media_error = error
                manager = None
                metadata = None

            if debug and metadata != last_debug:
                if metadata is None:
                    print("SMTC: no Spotify media session", flush=True)
                else:
                    title, artist, status, album, album_artist, subtitle, playback_type, duration = metadata
                    print(
                        f"SMTC: title={title!r}, artist={artist!r}, playback_status={status!s}, "
                        f"album={album!r}, album_artist={album_artist!r}, subtitle={subtitle!r}, "
                        f"playback_type={playback_type!s}, duration_seconds={duration!r}",
                        flush=True,
                    )
                last_debug = metadata

            if metadata is not None:
                title, artist, status = metadata[:3]
                album, duration = metadata[3], metadata[7]
                if status == media_control.GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING:
                    if is_ad(title, artist, album, duration):
                        mute.mute_current_sessions()  # Also catches newly spawned sessions.
                        if ad_started is None and mute.owned:
                            ad_started = time.monotonic()
                            print("Ad found; Spotify muted.", flush=True)
                            notify(notifications, "Spotify ad muted", "Spotify audio is muted until music resumes.")
                    elif album.strip() and (title.strip() or artist.strip()):
                        finish_ad("Music resumed; Spotify audio restored.")
            # Paused, stopped, and missing metadata do not imply a new track.
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        finish_ad("Stopping; Spotify audio restored.")
        mute.restore()


def main() -> int:
    parser = argparse.ArgumentParser(description="Mute Spotify desktop audio during ads.")
    parser.add_argument("--notify", action="store_true", help="Show Windows desktop notifications")
    parser.add_argument("--debug", action="store_true", help="Print Spotify SMTC metadata on every change")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This script requires Windows 10 or 11.")

    try:
        instance = SingleInstance()
    except (OSError, RuntimeError) as exc:
        print(exc, flush=True)
        return 1
    try:
        asyncio.run(run(args.debug, args.notify))
    except KeyboardInterrupt:
        print("Stopped. Spotify audio restored.", flush=True)
    except ImportError as exc:
        print(f"Missing dependency: {exc}. Run: pip install -r requirements.txt", flush=True)
        return 1
    finally:
        instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
