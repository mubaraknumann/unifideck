"""utils/proc_status.py — read this process's own ``/proc/self/status``.

Shared by the support-bundle device probe (which wants the kernel's raw
strings for a capture-time snapshot) and the memory sampler (which wants
numbers it can put in a time series). One parser so the two can never
disagree about which fields matter or how they are read.

**Strictly observational**, like ``support_bundle/procscan``: this reads
``/proc`` and nothing else. Everything it returns is a count or a size,
never a title, id or path, which is what keeps it safe in a bundle a
reporter pastes in public.

Linux-only by construction. On a kernel without ``/proc`` (or if the file
is unreadable) every function returns an empty mapping rather than
raising — a diagnostic that breaks the thing it is diagnosing is worse
than no diagnostic.
"""
from __future__ import annotations

from pathlib import Path

_STATUS_PATH = Path("/proc/self/status")

# The fields worth carrying. ``VmData`` and ``VmSwap`` are the
# load-bearing pair: a process that has grown its data segment and had
# most of it swapped out looks nothing like one that is merely
# resident-heavy, and telling those apart is what separates a retention
# leak from allocator fragmentation. ``Threads`` catches a thread leak,
# and the context-switch counters separate a spinning loop from an idle
# process.
STATUS_KEYS: tuple[str, ...] = (
    "VmPeak",
    "VmSize",
    "VmData",
    "VmRSS",
    "RssAnon",
    "VmSwap",
    "Threads",
    "voluntary_ctxt_switches",
    "nonvoluntary_ctxt_switches",
)


def read_status_raw(keys: tuple[str, ...] = STATUS_KEYS) -> dict[str, str]:
    """Return the requested fields as the kernel printed them.

    Values keep their units (``"1234 kB"``) so a bundle reader sees
    exactly what ``/proc`` said rather than a number this code decided to
    reinterpret.
    """
    try:
        text = _STATUS_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    wanted = set(keys)
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if sep and key in wanted:
            values[key] = rest.strip()
    return values


def read_status_numeric(keys: tuple[str, ...] = STATUS_KEYS) -> dict[str, int]:
    """Return the requested fields as plain ints, dropping units.

    ``Vm*`` and ``Rss*`` are in kB, ``Threads`` and the context-switch
    counters are bare counts; the unit suffix is stripped either way. A
    field that is absent or non-numeric is omitted rather than guessed
    at, so a caller charting a series never plots a fabricated zero.
    """
    numeric: dict[str, int] = {}
    for key, raw in read_status_raw(keys).items():
        head = raw.split(maxsplit=1)[0] if raw else ""
        try:
            numeric[key] = int(head)
        except ValueError:
            continue
    return numeric
