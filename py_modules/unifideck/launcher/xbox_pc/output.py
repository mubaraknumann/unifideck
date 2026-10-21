"""launcher/xbox_pc/output.py — read Xodus, Wine and game output safely.

``xodus-cli run``, ``xodus-service`` and the runtime's ``proton`` all write
to a pipe the launcher reads. Three rules keep that from hurting a launch:

* **Always drain.** A pipe nobody reads fills at 64 KiB and the writer
  blocks, which freezes the game. The pump reads even when no log file
  could be opened.
* **No line limit.** ``StreamReader.readline`` raises on a line over
  64 KiB, and Wine can print one. The pump reads 64 KiB chunks and splits
  lines itself; a line longer than 8 KiB is cut.
* **No secrets.** Anything token-shaped is redacted before it reaches the
  log or the tail that feeds the failure classifier.

``xodus-service`` logs a coloured ERROR on every normal disconnect (I-021).
Those lines are dropped.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import re
import time
from typing import IO

logger = logging.getLogger(__name__)

READ_CHUNK = 65536
MAX_LINE = 8192
TAIL_LINES = 200

_SECRET_RE = re.compile(
    r"(?:eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"
    r"|[A-Za-z0-9+/_=-]{120,}"
    r"|XBL3\.0 x=[^\s;]+;[^\s]+)",
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
#: I-021: ``Failed to read magic: ... early eof`` on each normal close.
_NOISE = ("Failed to read magic",)

#: ``(reason, lower-case markers)``, first match wins. The markers come
#: from the backend's diagnostics; an unknown failure is ``launch_failed``.
_FAILURE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sign_in_required", (
        "not signed in", "sign in again", "no stored account",
        "credential", "user-session", "invalid_grant",
    )),
    ("license_denied", (
        "license denied", "licence denied", "not entitled",
        "content-license 401", "content-license 403",
        "content-parsed 401", "content-parsed 403",
    )),
)


def redact(line: str) -> str:
    """``line`` with every token-shaped run replaced by ``[redacted]``."""
    return _SECRET_RE.sub("[redacted]", line)


def filter_line(line: str) -> str | None:
    """The line to keep, without colour codes, or None to drop it."""
    plain = _ANSI_RE.sub("", line).rstrip("\r\n")
    if not plain.strip():
        return None
    if any(marker in plain for marker in _NOISE):
        return None
    return plain


def classify_failure(tail: str) -> str:
    """The refusal reason that best explains a failed run's output."""
    lowered = tail.lower()
    for reason, markers in _FAILURE_MARKERS:
        if any(marker in lowered for marker in markers):
            return reason
    return "launch_failed"


class OutputSink:
    """Where every pumped line goes: the game log (if any) and a tail.

    The launcher runs one event loop, so pumps never write at the same
    time and the sink needs no lock.
    """

    def __init__(self, log_file: IO[str] | None, *, tail_lines: int = TAIL_LINES) -> None:
        """``log_file`` may be None; output is still drained and tailed."""
        self._log = log_file
        self._tail: collections.deque[str] = collections.deque(maxlen=tail_lines)

    def write(self, line: str, *, source: str = "") -> None:
        """Filter, redact and record one line."""
        kept = filter_line(line)
        if kept is None:
            return
        text = redact(kept)
        self._tail.append(text)
        if self._log is None:
            return
        stamp = time.strftime("%H:%M:%S")
        prefix = f"[{source}] " if source else ""
        try:
            self._log.write(f"[{stamp}] {prefix}{text}\n")
            self._log.flush()
        except (OSError, ValueError):
            # The log is a convenience. A full disk or a closed file must
            # not stop the drain, or the writer blocks on a full pipe.
            self._log = None

    def tail(self) -> str:
        """The last lines, oldest first."""
        return "\n".join(self._tail)

    async def pump(self, stream: asyncio.StreamReader | None, *, source: str) -> None:
        """Read ``stream`` to EOF in chunks and record each line."""
        if stream is None:
            return
        splitter = _LineSplitter()
        while True:
            chunk = await stream.read(READ_CHUNK)
            if not chunk:
                break
            for line in splitter.feed(chunk):
                self.write(line, source=source)
        rest = splitter.flush()
        if rest is not None:
            self.write(rest, source=source)


class _LineSplitter:
    """Split a byte stream into lines of at most :data:`MAX_LINE` bytes."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._skipping = False

    def feed(self, chunk: bytes) -> list[str]:
        """The complete lines in ``chunk`` plus what was buffered before it."""
        lines: list[str] = []
        parts = chunk.split(b"\n")
        for part in parts[:-1]:
            lines.extend(self._take(part, complete=True))
        lines.extend(self._take(parts[-1], complete=False))
        return lines

    def _take(self, part: bytes, *, complete: bool) -> list[str]:
        """Add ``part`` to the current line and emit it when done or too long."""
        out: list[str] = []
        if not self._skipping:
            self._buf.extend(part)
            if len(self._buf) > MAX_LINE:
                out.append(self._decode(self._buf[:MAX_LINE]) + " [cut]")
                self._buf.clear()
                self._skipping = True
        if complete:
            if not self._skipping and self._buf:
                out.append(self._decode(self._buf))
            self._buf.clear()
            self._skipping = False
        return out

    def flush(self) -> str | None:
        """The unterminated last line at EOF, or None."""
        if self._skipping or not self._buf:
            return None
        text = self._decode(self._buf)
        self._buf.clear()
        return text

    @staticmethod
    def _decode(data: bytes | bytearray) -> str:
        """Bytes to text; a broken sequence becomes U+FFFD."""
        return bytes(data).decode("utf-8", errors="replace")
