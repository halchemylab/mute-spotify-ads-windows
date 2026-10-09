"""A small, change-driven terminal view for the Spotify watcher."""

from collections import deque
from datetime import datetime
import os
import shutil
import sys


class TerminalDisplay:
    def __init__(self, stream=None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.interactive = self._enable_interactive()
        self.color = self.interactive and "NO_COLOR" not in os.environ
        self.events = deque(maxlen=5)
        self.state = ""
        self.track = ""
        self.detail = ""
        self._lines = 0
        self._last_frame = None

    def _enable_interactive(self) -> bool:
        if not self.stream.isatty() or os.environ.get("TERM", "").lower() == "dumb":
            return False
        if not self._fits():
            return False
        if sys.platform != "win32":
            return True
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetStdHandle.argtypes = (wintypes.DWORD,)
            kernel32.GetStdHandle.restype = wintypes.HANDLE
            kernel32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel32.GetConsoleMode.restype = wintypes.BOOL
            kernel32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.SetConsoleMode.restype = wintypes.BOOL
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = wintypes.DWORD()
            if handle == -1 or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
        except (AttributeError, OSError, ValueError):
            return False

    def _fits(self) -> bool:
        try:
            return shutil.get_terminal_size().columns >= 44 and all(
                self.stream.encoding and glyph.encode(self.stream.encoding)
                for glyph in "┌─┐│└┘—…●"
            )
        except (LookupError, UnicodeError, OSError):
            return False

    @staticmethod
    def _clean(value: str) -> str:
        # Metadata and exception strings must not inject terminal controls.
        return " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())

    @staticmethod
    def _clip(value: str, width: int) -> str:
        return value if len(value) <= width else value[: width - 1] + "…"

    def event(self, message: str, level: str = "INFO") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        message = self._clean(message)
        self.events.append((stamp, level, message))
        if not self.interactive:
            encoding = self.stream.encoding or "utf-8"
            line = f"[{stamp}] {level} {message}".encode(encoding, errors="replace").decode(encoding)
            print(line, file=self.stream, flush=True)

    def update(self, state: str, track: str = "", detail: str = "") -> None:
        state, track, detail = map(self._clean, (state, track, detail))
        changed = (state, track, detail) != (self.state, self.track, self.detail)
        self.state, self.track, self.detail = state, track, detail
        if self.interactive:
            self._render()
        elif changed:
            label = f"{state} | {track.replace(' — ', ' - ')}" if track else state
            if detail:
                label += f" | {detail}"
            self.event(label)

    def _render(self) -> None:
        if not self._fits():
            self.interactive = False
            self.event(self.state, "INFO")
            return
        width = min(shutil.get_terminal_size().columns - 1, 62)
        inner = width - 4
        accent = "\x1b[36m" if self.color else ""
        reset = "\x1b[0m" if self.color else ""

        def row(value: str, highlighted: bool = False) -> str:
            clipped = self._clip(value, inner)
            content = clipped
            if highlighted and self.color:
                content = f"{accent}{content}{reset}"
            return f"│ {content}{' ' * (inner - len(clipped))} │"

        heading = " SPOTIFY / AD MUTER "
        lines = [
            "┌─" + heading + "─" * (width - len(heading) - 3) + "┐",
            row("●  " + self.state.upper(), True),
        ]
        if self.track:
            lines.append(row("   " + self.track))
        if self.detail:
            lines.append(row("   " + self.detail))
        lines.extend(("└" + "─" * (width - 2) + "┘", " RECENT"))
        lines.extend(f" {stamp}  {self._clip(message, width - 12)}" for stamp, _, message in self.events)
        frame = tuple(lines)
        if frame == self._last_frame:
            return
        if self._lines:
            self.stream.write(f"\x1b[{self._lines}F")
        for line in lines:
            self.stream.write("\x1b[2K" + line + "\n")
        for _ in range(self._lines - len(lines)):
            self.stream.write("\x1b[2K\n")
        if self._lines > len(lines):
            self.stream.write(f"\x1b[{self._lines - len(lines)}F")
        self.stream.flush()
        self._lines = len(lines)
        self._last_frame = frame
