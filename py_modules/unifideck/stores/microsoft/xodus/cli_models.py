"""stores/microsoft/xodus/cli_models.py — pure parsers for xodus-cli output.

No I/O and no processes. ``cli.py`` runs the commands and hands their
stdout here; the install code (``progress.py``) feeds stdout lines to
``parse_progress_line``.

Three outputs carry the account email (``account``, ``library`` and
``profile``). The email is kept in memory only. It is hidden from
``repr`` and never logged, and ``redact`` removes it from any text that
does get logged.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: Marks a progress record on a ``streaming --progress-json`` stdout line.
PROGRESS_MARK = "NEXUS_PROGRESS {"
PROGRESS_SCHEMA = 1
#: Reads one JSON value and ignores whatever follows it on the line.
_DECODER = json.JSONDecoder()

_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_\-.%]{40,}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
#: ``2026-10-20T18:51:54.508009681+00:00``: 9 fraction digits.
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


class ParseError(ValueError):
    """Output that does not have the expected shape."""

    def __init__(self, code: str) -> None:
        """``code`` is a short stable reason, e.g. ``bad_json``."""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AccountStatus:
    """``xodus-cli account --json``: is an account stored."""

    stored: bool
    #: The account email. Memory only: never logged, never in a payload.
    username: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Profile:
    """``xodus-cli profile --json``. The gamerpic is dropped on read."""

    gamertag: str | None
    has_picture: bool


@dataclass(frozen=True)
class ProgressRecord:
    """One ``NEXUS_PROGRESS`` record of a streaming install."""

    sequence: int
    phase: str
    completed_bytes: int
    total_bytes: int | None
    bytes_per_second: int
    paused: bool

    @property
    def complete(self) -> bool:
        """The final record of a finished download."""
        return self.phase == "complete"


def redact(text: str) -> str:
    """``text`` with tokens and email addresses removed."""
    text = _LONG_TOKEN_RE.sub("[redacted]", text)
    return _EMAIL_RE.sub("[email]", text)


def last_json_object(stdout: str) -> dict[str, Any] | None:
    """The last stdout line that parses as a JSON object, or None.

    Commands print one JSON line, but a warning can come before it.
    """
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _require_object(stdout: str) -> dict[str, Any]:
    data = last_json_object(stdout)
    if data is None:
        raise ParseError("bad_json")
    return data


def parse_account(stdout: str) -> AccountStatus:
    """``{"stored": true, "username": ...}`` or ``{"stored": false}``."""
    data = _require_object(stdout)
    stored = data.get("stored")
    if not isinstance(stored, bool):
        raise ParseError("bad_schema")
    username = data.get("username")
    return AccountStatus(stored=stored, username=username if isinstance(username, str) else None)


def parse_profile(stdout: str) -> Profile:
    """``{"schema": 1, "account", "gamertag", "gamerpicDataUrl"}``."""
    data = _require_object(stdout)
    if data.get("schema") != 1:
        raise ParseError("bad_schema")
    tag = data.get("gamertag")
    gamertag = tag.strip() if isinstance(tag, str) and tag.strip() else None
    picture = data.get("gamerpicDataUrl")
    return Profile(gamertag=gamertag, has_picture=isinstance(picture, str) and bool(picture))


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _record_from(data: dict[str, Any]) -> ProgressRecord | None:
    sequence = _int_or_none(data.get("sequence"))
    phase = data.get("phase")
    if data.get("schema") != PROGRESS_SCHEMA or sequence is None or not isinstance(phase, str):
        return None
    return ProgressRecord(
        sequence=sequence,
        phase=phase,
        completed_bytes=_int_or_none(data.get("completedBytes")) or 0,
        total_bytes=_int_or_none(data.get("totalBytes")),
        bytes_per_second=_int_or_none(data.get("bytesPerSecond")) or 0,
        paused=data.get("paused") is True,
    )


def parse_progress_line(line: str, *, after: int = -1) -> ProgressRecord | None:
    """The progress record in ``line``, or None.

    The mark may appear anywhere in the line, because a line can carry
    other output first. Text after the record is ignored. A record is accepted only with schema 1 and a
    sequence above ``after`` (the last one accepted), so a repeated or
    stale record is dropped.
    """
    start = line.find(PROGRESS_MARK)
    if start < 0:
        return None
    try:
        data, _end = _DECODER.raw_decode(line, start + len(PROGRESS_MARK) - 1)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    record = _record_from(data)
    if record is None or record.sequence <= after:
        return None
    return record


def parse_rfc3339(text: Any) -> datetime | None:
    """An RFC 3339 timestamp as an aware ``datetime``, or None.

    Xodus prints 9 fraction digits. ``fromisoformat`` accepts at most 6 on
    some Python versions, so extra digits are cut, never rounded. A ``Z``
    suffix means UTC. A timestamp with no offset is refused.
    """
    if not isinstance(text, str) or not text:
        return None
    value = _FRACTION_RE.sub(r"\1", text.strip())
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
