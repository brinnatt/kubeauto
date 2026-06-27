"""
Render Docker Engine pull/push JSON streams like moby/client/pkg/jsonmessage.

Reference: https://github.com/moby/moby/tree/master/client/pkg/jsonmessage
"""
from __future__ import annotations

import io
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Optional, TextIO

from common.exceptions import CommandExecutionError

ANSI_ERASE_LINE = "\x1b[2K"
ANSI_CURSOR_UP = "\x1b[{n}A"
ANSI_CURSOR_DOWN = "\x1b[{n}B"

_SIZE_SUFFIXES = ("B", "kB", "MB", "GB", "TB", "PB")


def human_size(size: float) -> str:
    """Decimal byte sizes, aligned with github.com/docker/go-units HumanSize."""
    if size < 0:
        size = 0
    value = float(size)
    index = 0
    while value >= 1000 and index < len(_SIZE_SUFFIXES) - 1:
        value /= 1000
        index += 1
    return f"{value:.4g}{_SIZE_SUFFIXES[index]}"


def render_tui_progress(
    current: int,
    total: int,
    *,
    width: int = 200,
    start: int = 0,
    units: str = "",
    hide_counts: bool = False,
    now: Optional[float] = None,
) -> str:
    """Port of moby RenderTUIProgress."""
    if current <= 0 and total <= 0:
        return ""

    if total <= 0:
        if not units:
            return f"{human_size(float(current)):>8}"
        return f"{current} {units}"

    percentage = min(int(current / total * 100) // 2, 50)
    bar = ""
    if width > 110:
        spaces = max(50 - percentage, 0)
        bar = f"[{'=' * percentage}>{' ' * spaces}] "

    if hide_counts:
        numbers = ""
    elif not units:
        current_s = human_size(float(current))
        total_s = human_size(float(total))
        numbers = f"{current_s:>8}/{total_s}"
        if current > total:
            numbers = f"{current_s:>8}"
    else:
        numbers = f"{current}/{total} {units}"
        if current > total:
            numbers = f"{current} {units}"

    eta = ""
    if width > 50 and current > 0 and start > 0 and percentage < 50:
        elapsed = (now if now is not None else time.time()) - start
        if elapsed > 0:
            remaining = (total - current) * (elapsed / current)
            eta = " " + _format_duration(remaining)

    return bar + numbers + eta


def _format_duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes}m{sec}s"


def _terminal_width(default: int = 200) -> int:
    try:
        return shutil.get_terminal_size(fallback=(default, 24)).columns
    except OSError:
        return default


def _write_cursor_up(out: TextIO, lines: int) -> None:
    if lines:
        out.write(ANSI_CURSOR_UP.format(n=lines))


def _write_cursor_down(out: TextIO, lines: int) -> None:
    if lines:
        out.write(ANSI_CURSOR_DOWN.format(n=lines))


def _clear_line(out: TextIO) -> None:
    out.write(ANSI_ERASE_LINE)


def _stream_error(line: dict) -> Optional[str]:
    detail = line.get("errorDetail")
    if isinstance(detail, dict):
        message = detail.get("message")
        if message:
            return str(message)
    if line.get("error"):
        return str(line["error"])
    return None


@dataclass
class DockerJSONMessageDisplay:
    """Display pull/push JSON stream messages to a terminal or plain text output."""

    out: TextIO = sys.stderr

    def __post_init__(self) -> None:
        self._ids: dict[str, int] = {}
        self._is_terminal = bool(getattr(self.out, "isatty", lambda: False)())
        self._width = _terminal_width()

    def display_stream(self, lines: Iterable[dict]) -> None:
        for line in lines:
            self.display(line)
        if self._is_terminal:
            self.out.write("\n")
            self.out.flush()

    def display(self, line: dict) -> None:
        error = _stream_error(line)
        if error:
            raise CommandExecutionError(error)

        if line.get("aux") is not None:
            return

        layer_id = line.get("id") or ""
        has_progress = "progressDetail" in line

        diff = 0
        if layer_id and has_progress:
            if layer_id not in self._ids:
                self._ids[layer_id] = len(self._ids)
                if self._is_terminal:
                    self.out.write("\n")
            diff = len(self._ids) - self._ids[layer_id]
            if self._is_terminal:
                _write_cursor_up(self.out, diff)
        else:
            self._ids = {}

        progress = line.get("progressDetail")
        self._render_message(
            layer_id=layer_id,
            status=line.get("status") or "",
            stream=line.get("stream") or "",
            progress=progress if isinstance(progress, dict) else None,
            has_progress=has_progress,
        )

        if layer_id and self._is_terminal:
            _write_cursor_down(self.out, diff)
        self.out.flush()

    def _render_message(
        self,
        *,
        layer_id: str,
        status: str,
        stream: str,
        progress: Optional[dict],
        has_progress: bool,
    ) -> None:
        current = int((progress or {}).get("current") or 0)
        total = int((progress or {}).get("total") or 0)
        start = int((progress or {}).get("start") or 0)
        units = str((progress or {}).get("units") or "")
        hide_counts = bool((progress or {}).get("hidecounts"))

        endl = ""
        if self._is_terminal and not stream and has_progress:
            _clear_line(self.out)
            endl = "\r"
        elif has_progress and (current > 0 or total > 0):
            return

        if layer_id:
            self.out.write(f"{layer_id}: ")

        if has_progress and self._is_terminal:
            rendered = render_tui_progress(
                current,
                total,
                width=self._width,
                start=start,
                units=units,
                hide_counts=hide_counts,
            )
            self.out.write(f"{status} {rendered}{endl}")
        elif stream:
            self.out.write(f"{stream}{endl}")
        else:
            self.out.write(f"{status}\n")


def _run_self_tests() -> None:
    assert human_size(0) == "0B"
    assert human_size(1500) == "1.5kB"
    assert human_size(52_428_800).endswith("MB")

    bar = render_tui_progress(25_000_000, 50_000_000, width=120, start=int(time.time()) - 10)
    assert "[" in bar and "MB" in bar
    assert render_tui_progress(0, 0) == ""

    buf = io.StringIO()
    DockerJSONMessageDisplay(out=buf).display({"status": "Pulling from library/alpine", "id": "latest"})
    assert "Pulling from library/alpine" in buf.getvalue()

    buf = io.StringIO()
    display = DockerJSONMessageDisplay(out=buf)
    display._is_terminal = False
    display.display(
        {
            "status": "Downloading",
            "id": "abc123",
            "progressDetail": {"current": 1_048_576, "total": 52_428_800},
        }
    )
    assert buf.getvalue() == ""

    buf = io.StringIO()
    display = DockerJSONMessageDisplay(out=buf)
    display._is_terminal = False
    display.display({"status": "Download complete", "id": "abc123", "progressDetail": {}})
    assert "Download complete" in buf.getvalue()

    buf = io.StringIO()
    display = DockerJSONMessageDisplay(out=buf)
    display._is_terminal = True
    display._width = 120
    display.display(
        {
            "status": "Downloading",
            "id": "layer1",
            "progressDetail": {"current": 25_000_000, "total": 50_000_000, "start": int(time.time())},
        }
    )
    rendered = buf.getvalue()
    assert "layer1:" in rendered
    assert "Downloading" in rendered
    assert "MB" in rendered

    try:
        DockerJSONMessageDisplay(out=io.StringIO()).display({"errorDetail": {"message": "denied"}})
        raise AssertionError("expected CommandExecutionError")
    except CommandExecutionError as exc:
        assert "denied" in str(exc)

    print("common.docker_jsonmessage: all self-tests passed")


if __name__ == "__main__":
    _run_self_tests()
