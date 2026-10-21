"""Which Xbox PC titles the account may install: the entitlement filter.

A port of Xodus Nexus ``src/main/account-library.ts`` (``parseEntitlements``
and ``entitlementKind``). The input is one ``xodus-cli library --json``
answer::

    {"schema": 1, "account": "<email>", "market": "IN",
     "checkedAt": "2026-10-20T18:51:54.508009681+00:00",
     "skippedEntitlements": 0,
     "entitlements": [{"productId", "status", "isTrial", "special",
                       "sharingSource", "endDate", "satisfiedByProductId"}]}

The rules are the same as in Nexus:

- A row with an invalid product id is skipped, but its id is not blocked.
- Two rows with one id block both copies. A row with a bad field is
  blocked. Every descendant of a blocked id (through
  ``satisfiedByProductId``) is dropped too, so a bad parent never turns a
  child's reference into access.
- Access needs ``Active``, not a trial, not special, ``sharingSource ==
  "None"`` and an ``endDate`` in the future, on the row and on every parent.

Two things differ from Nexus on purpose. Nexus hides rows under a plan it
does not know (EA Play, Ubisoft+ and any other ``CFQ...`` plan). Here they
get their own access kind, so the library can list them as "not supported
yet". And the ``account`` field is the user's email: it is kept only as a
hash, and never logged or stored in clear.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

PRODUCT_ID_RE = re.compile(r"^[A-Z0-9]{12}\Z")

#: The Game Pass plans Nexus knows: PC Game Pass, Ultimate, Premium,
#: Essential, Game Pass for Console and Starter.
KNOWN_PLANS = frozenset({
    "CFQ7TTC0KGQ8", "CFQ7TTC0KHS0", "CFQ7TTC0P85B",
    "CFQ7TTC0K5DJ", "CFQ7TTC0K6L8", "CFQ7TTC10QFD",
})
EA_PLAY = "CFQ7TTC0K5DH"
#: Seen as the parent of every Ubisoft title. It has no catalog entry.
UBISOFT_PLUS = "CFQ7TTBZT8LN"
PLAN_PREFIX = "CFQ"

#: The access kinds an install may use. The rest are listed as not supported.
INSTALL_ACCESS = frozenset({"owned", "gamepass"})

MAX_OUTPUT_CHARS = 8_000_000
MAX_ROWS = 10_000
MAX_SKIPPED = 200_000
#: An answer is fresh for 5 minutes after ``checkedAt``, with 60 s of skew.
FRESH_FOR_S = 300.0
CLOCK_SKEW_S = 60.0
#: How many parents ``access_of`` follows before it gives up.
CHAIN_LIMIT = 16
_SHORT_TEXT = 40
_DATE_TEXT = 50

_FRACTION_RE = re.compile(r"(\.\d{1,6})\d*")


class EnvelopeError(ValueError):
    """The ``library`` answer as a whole is unusable. ``code`` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Entitlement:
    """One valid entitlement row. ``end_date`` is epoch seconds."""

    product_id: str
    status: str
    is_trial: bool
    special: bool
    sharing_source: str
    end_date: float
    end_date_text: str
    parent: str | None

    def usable(self, now: float) -> bool:
        """Whether this row alone grants access at ``now``."""
        return (
            self.status == "Active" and not self.is_trial and not self.special
            and self.sharing_source == "None" and self.end_date > now
        )


@dataclass(frozen=True)
class EntitlementSnapshot:
    """A parsed ``library`` answer. ``skipped`` counts rows not usable."""

    account_hash: str
    market: str
    checked_at: float
    entries: dict[str, Entitlement]
    skipped: int


def account_hash(account: str) -> str:
    """The SHA-256 of the normalised account name. The name is never kept."""
    return hashlib.sha256(account.strip().lower().encode("utf-8")).hexdigest()


def parse_rfc3339(text: str) -> float | None:
    """Epoch seconds of an RFC 3339 time, or None.

    Accepts ``Z`` and fractions longer than 6 digits (Xodus writes 9),
    which are truncated to microseconds.
    """
    if not isinstance(text, str) or not text:
        return None
    value = _FRACTION_RE.sub(lambda m: m.group(1), text.strip(), count=1)
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError):
        return None


def _envelope(stdout: str, market: str, now: float) -> dict[str, Any]:
    """The checked JSON object of a ``library`` answer."""
    if len(stdout) > MAX_OUTPUT_CHARS:
        raise EnvelopeError("too_large")
    lines = [ln for ln in stdout.splitlines() if ln.lstrip().startswith("{")]
    try:
        data = json.loads(lines[-1]) if lines else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise EnvelopeError("bad_json")
    rows = data.get("entitlements")
    if data.get("schema") != 1 or not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise EnvelopeError("bad_envelope")
    if not isinstance(data.get("account"), str) or data.get("market") != market:
        raise EnvelopeError("bad_envelope")
    checked = parse_rfc3339(data.get("checkedAt", ""))
    if checked is None or not -CLOCK_SKEW_S <= now - checked < FRESH_FOR_S:
        raise EnvelopeError("stale")
    skipped = data.get("skippedEntitlements")
    if skipped is not None and (
        isinstance(skipped, bool) or not isinstance(skipped, int)
        or not 0 <= skipped <= MAX_SKIPPED
    ):
        raise EnvelopeError("bad_skipped")
    return data


def _row(row: dict[str, Any]) -> Entitlement | None:
    """The row as an ``Entitlement``, or None when a field is invalid."""
    status, sharing = row.get("status"), row.get("sharingSource")
    end_text, parent = row.get("endDate"), row.get("satisfiedByProductId")
    if not isinstance(status, str) or len(status) >= _SHORT_TEXT:
        return None
    if not isinstance(sharing, str) or len(sharing) >= _SHORT_TEXT:
        return None
    if not isinstance(row.get("isTrial"), bool) or not isinstance(row.get("special"), bool):
        return None
    if not isinstance(end_text, str) or len(end_text) >= _DATE_TEXT:
        return None
    end = parse_rfc3339(end_text)
    if end is None or (parent is not None and not is_product_id(parent)):
        return None
    return Entitlement(
        product_id=row["productId"], status=status, is_trial=row["isTrial"],
        special=row["special"], sharing_source=sharing, end_date=end,
        end_date_text=end_text, parent=parent or None,
    )


def is_product_id(value: Any) -> bool:
    """Whether ``value`` is an upper-case 12-character product id."""
    return isinstance(value, str) and bool(PRODUCT_ID_RE.match(value))


def _quarantine(entries: dict[str, Entitlement], blocked: set[str]) -> None:
    """Drop every descendant of a blocked id from ``entries`` (BFS)."""
    children: dict[str, list[str]] = {}
    for pid, row in entries.items():
        if row.parent:
            children.setdefault(row.parent, []).append(pid)
    queue = list(blocked)
    index = 0
    while index < len(queue):
        for child in children.get(queue[index], []):
            if child not in blocked:
                blocked.add(child)
                queue.append(child)
                entries.pop(child, None)
        index += 1


def parse_library_output(stdout: str, *, market: str, now: float) -> EntitlementSnapshot:
    """Parse and validate one ``library --json`` answer.

    Raises ``EnvelopeError`` when the answer as a whole is unusable: too
    large, not JSON, the wrong schema or market, stale, or a bad skipped
    count. Bad rows never fail the answer; they are counted in ``skipped``.
    """
    data = _envelope(stdout, market, now)
    rows = data["entitlements"]
    entries: dict[str, Entitlement] = {}
    seen: set[str] = set()
    blocked: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict) or not is_product_id(raw.get("productId")):
            continue
        pid = raw["productId"]
        duplicate = pid in seen
        seen.add(pid)
        row = None if duplicate else _row(raw)
        if row is None:
            blocked.add(pid)
            entries.pop(pid, None)
            continue
        entries[pid] = row
    _quarantine(entries, blocked)
    reported = data.get("skippedEntitlements") or 0
    skipped = min(MAX_SKIPPED, reported + len(rows) - len(entries))
    return EntitlementSnapshot(
        account_hash=account_hash(data["account"]), market=market,
        checked_at=parse_rfc3339(data["checkedAt"]) or now,
        entries=entries, skipped=skipped,
    )


def plan_access(plan_id: str) -> str | None:
    """The access kind a plan parent grants, or None for a product parent."""
    if plan_id in KNOWN_PLANS:
        return "gamepass"
    if plan_id == EA_PLAY:
        return "ea_play"
    if plan_id == UBISOFT_PLUS:
        return "ubisoft_plus"
    if plan_id.startswith(PLAN_PREFIX):
        return "unknown_plan"
    return None


def access_of(
    pid: str, entries: dict[str, Entitlement], now: float,
    visited: frozenset[str] = frozenset(),
) -> str | None:
    """How the account may use ``pid``: ``owned``, ``gamepass``, ``ea_play``,
    ``ubisoft_plus``, ``unknown_plan``, or None for no access.

    Every row along the parent chain must be usable. A product parent passes
    its own access down, up to ``CHAIN_LIMIT`` steps; a missing parent gives
    no access. A row with no parent is a purchase, unless it is a plan.
    """
    if pid in visited or len(visited) >= CHAIN_LIMIT:
        return None
    row = entries.get(pid)
    if row is None or not row.usable(now):
        return None
    if not row.parent:
        return None if pid.startswith(PLAN_PREFIX) else "owned"
    kind = plan_access(row.parent)
    if kind is not None:
        return kind
    return access_of(row.parent, entries, now, visited | {pid})


def plan_id_of(pid: str, entries: dict[str, Entitlement]) -> str | None:
    """The plan at the top of ``pid``'s parent chain, or None for a purchase."""
    current: str | None = pid
    for _ in range(CHAIN_LIMIT):
        row = entries.get(current) if current else None
        if row is None or not row.parent:
            return None
        if row.parent.startswith(PLAN_PREFIX):
            return row.parent
        current = row.parent
    return None
