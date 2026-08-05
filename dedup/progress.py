"""Dependency-free progress reporting to stderr.

Interactive terminals get a single line that updates in place (carriage
return). Non-tty output (redirected to a log file, CI, etc.) gets periodic
newline-terminated snapshots instead, so it doesn't fill a log with
carriage-return noise.
"""

from __future__ import annotations

import shutil
import sys
import time
from typing import Optional


def human_size(n: float) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class Progress:
    def __init__(
        self,
        label: str,
        total: Optional[int] = None,
        total_bytes: Optional[int] = None,
        enabled: bool = True,
        stream=sys.stderr,
    ):
        self.label = label
        self.total = total
        self.total_bytes = total_bytes
        self.enabled = enabled
        self.stream = stream
        self.count = 0
        self.bytes_done = 0
        self.extra_counts: dict[str, int] = {}
        self._is_tty = bool(getattr(stream, "isatty", lambda: False)())
        self._interval = 0.15 if self._is_tty else 2.0
        self._last_print = 0.0
        self._printed_anything = False

    def advance(self, n: int = 1, nbytes: int = 0, current: str = "", **extra_counts: int) -> None:
        self.count += n
        self.bytes_done += nbytes
        for k, v in extra_counts.items():
            self.extra_counts[k] = self.extra_counts.get(k, 0) + v
        if not self.enabled:
            return
        now = time.monotonic()
        done = self.total is not None and self.count >= self.total
        if now - self._last_print >= self._interval or done:
            self._render(current)
            self._last_print = now

    def _render(self, current: str) -> None:
        parts = [self.label]
        if self.total:
            pct = 100 * self.count / self.total
            parts.append(f"{self.count}/{self.total} ({pct:.0f}%)")
        else:
            parts.append(f"{self.count} seen")
        if self.total_bytes:
            parts.append(f"{human_size(self.bytes_done)}/{human_size(self.total_bytes)}")
        elif self.bytes_done:
            parts.append(human_size(self.bytes_done))
        for k in sorted(self.extra_counts):
            parts.append(f"{k}={self.extra_counts[k]}")
        line = " | ".join(p for p in parts if p)
        if current:
            line += f" | {current}"

        if self._is_tty:
            width = shutil.get_terminal_size((100, 20)).columns
            line = line[: width - 1]
            print("\r" + line.ljust(width - 1), end="", file=self.stream, flush=True)
        else:
            print(line, file=self.stream, flush=True)
        self._printed_anything = True

    def finish(self, current: str = "") -> None:
        if not self.enabled:
            return
        self._render(current)
        if self._is_tty and self._printed_anything:
            print(file=self.stream)
