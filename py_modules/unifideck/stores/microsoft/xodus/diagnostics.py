"""stores/microsoft/xodus/diagnostics.py — the stderr side of an Xodus run.

Xodus prints its machine-readable diagnostics to stderr with ``eprintln!``::

    NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-http 403
    NEXUS_CREDENTIAL_ERROR_V1 keyring-missing-data

Stderr is unbuffered and one line can leave in several writes, so it is
read on its own pipe, never merged into stdout. Merged, a progress record
could land inside a diagnostic line and both would be lost.

:func:`drain_stderr` runs next to the stdout drain for the whole run. It
keeps every diagnostic line for :mod:`.errors` and puts a redacted copy of
every line in the tail buffer that feeds the failure message.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass
from typing import Any

from unifideck.stores.shared.cli_install_helpers import TailRingBuffer

from .cli_models import redact as redact_tokens

logger = logging.getLogger(__name__)

READ_CHUNK = 65536
MAX_LINE = 8192
TAIL_CHARS = 300
#: Diagnostics kept per run. Nexus reads the last 256 lines too.
MAX_DIAGNOSTICS = 256

_QUERY_RE = re.compile(r"(https?://[^\s?]+)\?\S*")
_LICENSE_RE = re.compile(
    r"NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 ([a-z0-9-]{1,60}) (0|[1-5][0-9]{2})\s*\Z",
)
_CREDENTIAL_RE = re.compile(r"NEXUS_CREDENTIAL_ERROR_V1 ([a-z0-9-]{1,60})\s*\Z")


def redact(text: str) -> str:
    """``text`` without URL query strings, tokens or email addresses."""
    return redact_tokens(_QUERY_RE.sub(r"\1?[query]", text))


@dataclass(frozen=True)
class LicenseDiagnostic:
    """One content-license stage. ``status`` is 0 when no HTTP reply came."""

    stage: str
    status: int


def parse_license(line: str) -> LicenseDiagnostic | None:
    """The content-license diagnostic in ``line``, or None."""
    match = _LICENSE_RE.search(line)
    if match is None:
        return None
    return LicenseDiagnostic(match.group(1), int(match.group(2)))


def parse_credential(line: str) -> str | None:
    """The credential error code in ``line`` (``keyring-missing-data``), or None."""
    match = _CREDENTIAL_RE.search(line)
    return match.group(1) if match else None


class Diagnostics:
    """The ``NEXUS_*_V1`` lines of one run, newest last."""

    def __init__(self) -> None:
        """Start empty."""
        self.lines: deque[str] = deque(maxlen=MAX_DIAGNOSTICS)

    def add(self, line: str) -> None:
        """Keep ``line`` when it is a diagnostic."""
        if "NEXUS_" in line and "_V1 " in line:
            self.lines.append(line.strip())

    def licenses(self) -> list[LicenseDiagnostic]:
        """Every content-license stage, in order."""
        found = (parse_license(line) for line in self.lines)
        return [diag for diag in found if diag is not None]

    def credential_errors(self) -> list[str]:
        """Every credential error code, in order."""
        found = (parse_credential(line) for line in self.lines)
        return [code for code in found if code is not None]


def _keep(line: str, tail: TailRingBuffer, diags: Diagnostics) -> None:
    """Record one stderr line."""
    line = line.strip()
    if not line:
        return
    diags.add(line)
    tail.append(redact(line)[:TAIL_CHARS])


async def drain_stderr(stream: Any, tail: TailRingBuffer, diags: Diagnostics) -> None:
    """Read ``stream`` to EOF, keeping diagnostics and a redacted tail.

    Reads fixed chunks and splits lines itself, so a line longer than the
    reader's limit cannot raise. A line longer than ``MAX_LINE`` is cut.
    """
    pending = b""
    while True:
        chunk = await stream.read(READ_CHUNK)
        if not chunk:
            break
        pending += chunk
        *lines, pending = pending.split(b"\n")
        for raw in lines:
            _keep(raw[:MAX_LINE].decode(errors="replace"), tail, diags)
        if len(pending) > MAX_LINE:
            _keep(pending[:MAX_LINE].decode(errors="replace"), tail, diags)
            pending = b""
    if pending:
        _keep(pending[:MAX_LINE].decode(errors="replace"), tail, diags)


async def stop_drain(task: asyncio.Task[None], *, grace_s: float = 5.0) -> None:
    """Let the stderr drain finish after the process ended, then cancel it.

    The process is gone by now, so EOF is near. The bound covers a child
    that inherited the pipe and keeps it open. When our own task is being
    cancelled, the drain is cancelled too and the cancel goes on.
    """
    try:
        if not task.done():
            await asyncio.wait({task}, timeout=grace_s)
    finally:
        if not task.done():
            task.cancel()
    # ``asyncio.wait`` never raises the task's own exception.
    await asyncio.wait({task})
    if not task.cancelled() and task.exception() is not None:
        logger.warning("[xbox_install] stderr drain failed: %s", task.exception())
