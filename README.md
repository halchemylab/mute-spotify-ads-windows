# Mute Spotify Ads for Windows

This script watches the **Spotify desktop app** through Windows media sessions (SMTC). When Spotify reports ad-like metadata while playing, it mutes Spotify's own Windows audio sessions. It restores those sessions when music resumes. It polls every 0.5 seconds, following the behavior of the [macOS script](https://github.com/gdi3d/mute-spotify-ads-mac-osx/blob/master/NoAdsSpotify.sh).

## Install

Use Windows 10 or 11 with **Python 3.10–3.14**. The dependencies use the `winrt-*` packages because `winsdk` does not provide wheels for recent Python versions.

```powershell
python -m pip install -r requirements.txt
```

Keep `mute_spotify_ads.py` and `requirements.txt` in the same folder. Start Spotify's desktop app (Microsoft Store or standalone installer).

## Run

```powershell
python mute_spotify_ads.py
python mute_spotify_ads.py --notify
python mute_spotify_ads.py --debug
```

`--debug` prints the raw title, artist, playback status, album fields, subtitle, playback type, and duration whenever Spotify's media session changes. Playback type may show `None` if its optional WinRT module is unavailable. To adjust ad matching, edit `is_ad()` and `MAX_AD_DURATION_SECONDS` in the script. The rule recognizes common ad labels and items with **no album** whose reported duration is **under 60 seconds**. It restores audio when a playing item has an album. It only acts while playback status is `Playing`; pausing does not change the mute state. Press **Ctrl+C** to stop and restore any Spotify sessions the script muted. A second copy of the script exits with an already-running message.

Terminal messages include a local time in `[HH:MM:SS]` format. When an ad ends, the restoration message shows how many seconds it was muted.

After each muted ad, the script adds its elapsed seconds to `stats.txt` beside the script and prints the total. To store it elsewhere, set `MUTE_SPOTIFY_STATS_PATH` to a **full file path**, for example:

```powershell
$env:MUTE_SPOTIFY_STATS_PATH = "$env:USERPROFILE\Documents\spotify-ad-stats.txt"
python mute_spotify_ads.py
```

## Start automatically at login

In Task Scheduler, create a basic task with the **When I log on** trigger. Set **Program/script** to the full path of `pythonw.exe` for the Python installation used above, and **Add arguments** to the full quoted path of `mute_spotify_ads.py` (optionally followed by `--notify`). Run it only when you are logged on, so it can access your desktop media and audio sessions. `pythonw.exe` keeps the console window hidden; use regular `python.exe --debug` when troubleshooting.

Alternatively, make a shortcut to that same `pythonw.exe` command in the Startup folder (`Win+R`, then `shell:startup`). Use only one startup method.

## Known limitations

- Spotify **desktop app only**; the web player is out of scope.
- Some ad audio can leak before the next poll detects it, usually up to about one second.
- Detection depends on Spotify's SMTC metadata and may need tuning with `--debug` as Spotify changes it. The album and duration rule comes from a small sample: a short song or podcast item with no album can be muted, while a longer ad or one with an album can be missed. Metadata fields can briefly carry over from the previous item, delaying detection or restoration by a poll.
- The stats count wall-clock seconds from successful muting until the next playing song (or shutdown). They include time if an ad is paused.
- An audio session already muted by you stays muted. If you manually change mute during an ad, the script may reapply mute until the ad ends.
- Windows notifications depend on the current desktop notification settings. Console messages and muting continue if a toast cannot be shown.

## Uninstall

Stop the running script, remove its Task Scheduler task or Startup shortcut if configured, then delete this folder. Delete the stats file too if you no longer want the history. To remove the installed dependencies from this Python environment:

```powershell
python -m pip uninstall pycaw psutil winrt-Windows.Media.Control winotify
```
