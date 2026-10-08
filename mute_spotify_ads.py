"""Mute Spotify's Windows audio sessions while its SMTC metadata indicates an ad."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
from datetime import datetime, timedelta
import json
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


def log(message: str, level: str = "INFO") -> None:
    print(f"[{datetime.now():%H:%M:%S}] {level} {message}", flush=True)


class PollErrors:
    """Show each polling error once until that operation succeeds again."""

    def __init__(self) -> None:
        self.active: dict[str, set[str]] = {}

    def report(self, operation: str, exc: Exception) -> None:
        detail = f"{type(exc).__name__}: {exc}"
        seen = self.active.setdefault(operation, set())
        if detail not in seen:
            log(f"{operation} failed: {detail}", "WARN")
            seen.add(detail)

    def recovered(self, operation: str) -> None:
        if self.active.pop(operation, None):
            log(f"{operation} recovered.")


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


def spotify_audio_sessions(audio_utilities, errors: PollErrors):
    """Return all current Spotify sessions, including sessions from child processes."""
    result = []
    inspection_failed = False
    for session in audio_utilities.GetAllSessions():
        try:
            process = session.Process
            if process and process.name().casefold() == "spotify.exe":
                result.append(session)
        except Exception as exc:
            errors.report("Audio session inspection", exc)
            inspection_failed = True
    if not inspection_failed:
        errors.recovered("Audio session inspection")
    return result


class SpotifyMute:
    def __init__(self, audio_utilities, errors: PollErrors) -> None:
        self.audio_utilities = audio_utilities
        self.errors = errors
        # Only entries that this script changed from unmuted to muted are owned.
        self.owned: dict[str, object] = {}

    def mute_current_sessions(self) -> None:
        try:
            sessions = spotify_audio_sessions(self.audio_utilities, self.errors)
            self.errors.recovered("Audio session list")
        except Exception as exc:
            self.errors.report("Audio session list", exc)
            return
        mute_failed = False
        mute_succeeded = False
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
                mute_succeeded = True
            except Exception as exc:
                self.errors.report("Spotify audio session mute", exc)
                mute_failed = True
        if mute_succeeded and not mute_failed:
            self.errors.recovered("Spotify audio session mute")

    def restore(self) -> None:
        for key, volume in list(self.owned.items()):
            try:
                volume.SetMute(0, None)
            except Exception as exc:
                # The owning Spotify process may already have exited.
                log(f"Could not restore a closed Spotify audio session: {exc}", "WARN")
            finally:
                del self.owned[key]


def read_stats_history() -> tuple[int, list[tuple[datetime, int]], bool]:
    """Read legacy total seconds and timestamped ad entries from stats.txt."""
    try:
        lines = STATS_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0, [], False
    except OSError as exc:
        log(f"Could not read stats from {STATS_PATH}: {exc}; starting at zero.", "WARN")
        return 0, [], False

    legacy_seconds = 0
    has_legacy = False
    entries: list[tuple[datetime, int]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            if number == 1 and line.strip().isdigit():
                legacy_seconds = int(line.strip())
                has_legacy = True
                continue
            item = json.loads(line)
            stamp = datetime.fromisoformat(item["at"])
            seconds = item["seconds"]
            if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
                raise ValueError("seconds must be a nonnegative integer")
            entries.append((stamp, seconds))
        except (ValueError, TypeError, KeyError) as exc:
            log(f"Ignoring invalid stats line {number} in {STATS_PATH}: {exc}", "WARN")
    return legacy_seconds, entries, has_legacy


def read_stats() -> int:
    legacy_seconds, entries, _ = read_stats_history()
    return legacy_seconds + sum(seconds for _, seconds in entries)


def add_stats(seconds: int) -> int:
    total = read_stats() + seconds
    try:
        STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        previous = STATS_PATH.read_text(encoding="utf-8") if STATS_PATH.exists() else ""
        entry = json.dumps({"at": datetime.now().astimezone().isoformat(), "seconds": seconds})
        with STATS_PATH.open("a", encoding="utf-8") as stats_file:
            if previous and not previous.endswith("\n"):
                stats_file.write("\n")
            stats_file.write(entry + "\n")
    except OSError as exc:
        log(f"Could not write stats to {STATS_PATH}: {exc}", "WARN")
    return total


def format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def show_stats() -> None:
    legacy_seconds, entries, has_legacy = read_stats_history()
    today = datetime.now().astimezone().date()
    week_start = today - timedelta(days=today.weekday())
    today_entries = [(at, seconds) for at, seconds in entries if at.astimezone().date() == today]
    week_entries = [(at, seconds) for at, seconds in entries if week_start <= at.astimezone().date() <= today]

    for label, selected in (("Today", today_entries), ("This week", week_entries), ("All time", entries)):
        duration = sum(seconds for _, seconds in selected)
        if label == "All time":
            duration += legacy_seconds
        count = len(selected)
        count_label = "recorded ads" if label == "All time" and has_legacy else "ads muted"
        print(f"{label + ':':<11}{count} {count_label} · {format_duration(duration)}")
    if has_legacy:
        print(f"All-time duration includes {format_duration(legacy_seconds)} from the previous stats file; its ad count and dates are unknown.")


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
        log(f"Could not show notification: {exc}", "WARN")


async def spotify_media(manager, errors: PollErrors):
    """Return Spotify media details, or None if no SMTC session exists."""
    read_failed = False
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
            if not read_failed:
                errors.recovered("Spotify media session read")
            return title, artist, playback, album, album_artist, subtitle, playback_type, duration
        except Exception as exc:
            errors.report("Spotify media session read", exc)
            read_failed = True
    if not read_failed:
        errors.recovered("Spotify media session read")
    return None


async def run(debug: bool, notifications: bool) -> None:
    from winrt.windows.media import control as media_control
    from pycaw.pycaw import AudioUtilities
    import psutil

    errors = PollErrors()
    mute = SpotifyMute(AudioUtilities, errors)
    ad_started: float | None = None
    unseen = object()
    last_debug = unseen
    spotify_was_running: bool | None = None
    manager = None

    def finish_ad(reason: str) -> None:
        nonlocal ad_started
        if ad_started is None:
            return
        mute.restore()
        elapsed = max(0, round(time.monotonic() - ad_started))
        ad_started = None
        total = add_stats(elapsed)
        message = total_message(total)
        minutes, seconds = divmod(total, 60)
        log(f"{reason.rstrip('.')} | ad muted: {elapsed}s | total: {minutes}m {seconds}s")
        if reason.startswith("Music resumed"):
            notify(notifications, "Spotify music resumed", message)

    log("Watching Spotify desktop ads. Press Ctrl+C to stop.")
    try:
        while True:
            try:
                running = spotify_running(psutil)
                errors.recovered("Spotify process check")
            except Exception as exc:
                errors.report("Spotify process check", exc)
                running = True  # Do not change mute state on an uncertain result.

            if not running:
                finish_ad("Spotify closed; its audio sessions were restored.")
                if spotify_was_running is not False:
                    log("Spotify is not running; waiting for it to start.")
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
                metadata = await spotify_media(manager, errors)
                errors.recovered("Windows media session query")
            except Exception as exc:
                errors.report("Windows media session query", exc)
                manager = None
                metadata = None

            if debug and metadata != last_debug:
                if metadata is None:
                    log("SMTC: no Spotify media session")
                else:
                    title, artist, status, album, album_artist, subtitle, playback_type, duration = metadata
                    log(
                        f"SMTC: title={title!r}, artist={artist!r}, playback_status={status!s}, "
                        f"album={album!r}, album_artist={album_artist!r}, subtitle={subtitle!r}, "
                        f"playback_type={playback_type!s}, duration_seconds={duration!r}",
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
                            log("Ad found; Spotify muted.")
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
    parser.add_argument("--stats", action="store_true", help="Show ad counts and muted time for today, this week, and all time")
    args = parser.parse_args()
    if args.stats:
        show_stats()
        return 0
    if sys.platform != "win32":
        parser.error("This script requires Windows 10 or 11.")

    try:
        instance = SingleInstance()
    except (OSError, RuntimeError) as exc:
        log(str(exc), "ERROR")
        return 1
    try:
        asyncio.run(run(args.debug, args.notify))
    except KeyboardInterrupt:
        log("Stopped. Spotify audio restored.")
    except ImportError as exc:
        log(f"Missing dependency: {exc}. Run: pip install -r requirements.txt", "ERROR")
        return 1
    finally:
        instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
