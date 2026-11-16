"""support_bundle/spec.py — Data shapes and size policy.

Pure declarations: the dataclasses the collector passes around and
every cap that bounds the archive. No logic, no I/O.

Caps are module constants rather than config keys on purpose. The
``logs`` block in ``config/schema.json`` is ``additionalProperties:
false``, so a new tunable would mean editing the schema, the bundled
defaults, and the defaults test — for numbers no user should need to
touch. The one path that *is* user-tunable (``logs.export_path``)
already exists in the schema and is honoured by ``resolve.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ── Size policy ───────────────────────────────────────────────────
# Completeness beats compactness. These caps exist only to stop a
# pathological case from producing an unusable archive — a game that
# spammed stderr into a multi-gigabyte log, or a runaway retry loop.
# They are deliberately far above anything a real device produces.
#
# The tradeoff is cheap in a way worth writing down: logs deflate
# roughly 7x, so a megabyte of extra raw content costs ~150 KB in the
# zip. An earlier, much tighter policy discarded 282 KB of real
# diagnostic content from a live capture to save about 39 KB
# compressed. That is a bad trade, and these numbers reflect it.
#
# Nothing is ever dropped silently: every truncation and every
# newest-N exclusion is recorded in the manifest and counted in the
# RPC result.
CAP_TOTAL_UNCOMPRESSED = 96 * 1024 * 1024
CAP_DEFAULT = 4 * 1024 * 1024
CAP_DECKY_SESSION = 8 * 1024 * 1024
CAP_GAME_LOG = 8 * 1024 * 1024
CAP_SMALL_JSON = 2 * 1024 * 1024
CAP_JSON_LARGE = 16 * 1024 * 1024
CAP_VDF = 16 * 1024 * 1024
# The browser log is the one place where the cap is a *privacy* lever
# rather than a size one: it is stderr from a live OAuth session, so
# it gets the aggressive scrub profile and the auth-line prefilter.
# Generous, but not unbounded.
CAP_EDGE_LOG = 2 * 1024 * 1024
# Steam rotates its own logs at 8 MB and keeps a .previous copy, so
# matching that number means a collected Steam log is never truncated -
# the file cannot exceed the cap in the first place. A 4 MB cap started
# clipping compat_log (the Proton-selection log) within a day of use.
# The .previous halves are enumerated in the inventory, not collected.
CAP_STEAM_LOG = 8 * 1024 * 1024

# Newest-N caps. These files are a couple of KB each, so the limits
# are high enough to be a backstop rather than a policy; anything they
# do exclude is listed in the manifest.
MAX_DECKY_FILES = 40
MAX_LAUNCH_LOGS = 400
MAX_GAME_LOGS = 400
MAX_PROTON_LOGS = 40

# Truncation banner. Byte counts are recorded in the manifest too,
# but a reader opening the file directly must see immediately that
# they are looking at a tail, not a whole log.
TRUNCATION_BANNER = (
    "...[TRUNCATED by Capture Logs: kept the last {kept} of {total} "
    "bytes. Logs are append-only, so the tail holds the failure. "
    "See manifest.json]\n"
)

# ── Vocabularies ──────────────────────────────────────────────────
# Where a source's path comes from. ``generated`` sources have no
# path at all — they are built in memory by the probes. ``paths``
# means "a named field on ServicePaths", so a user who relocated
# their data dir or runs a non-default Steam layout is audited at
# the location the plugin actually uses, not the default one.
Root = Literal[
    "decky_logs", "launches", "data", "config",
    "steam", "plugin", "home", "paths", "generated",
]

# What we do with a source once it is found.
#   include        — whole file, subject to max_bytes
#   tail           — last max_bytes, newline-aligned
#   presence_only  — stat() it, never read one byte (credentials)
#   bulk           — directory we only summarise (prefixes, profiles)
#   static         — shipped reference data, identical for every user
#   skip           — deliberately excluded, recorded with a reason
Policy = Literal["include", "tail", "presence_only", "bulk", "static", "skip"]

# Redaction profile applied to a collected byte stream.
Scrub = Literal["none", "text", "text_aggressive", "text_ea", "json", "jsonl"]

# Whether a missing file is suspicious. This is what makes the audit
# readable: absent Amazon state on a device with no Amazon account is
# normal, absent shortcuts.vdf never is.
#   always        — must exist on a healthy install
#   optional      — transient or feature-gated, absence means nothing
#   sync          — appears after the first library sync
#   launch        — appears after the first game launch
#   <store name>  — appears only when that store is connected
Expect = str

# Verdict of a derived sanity check.
CheckStatus = Literal["pass", "fail", "warn", "na", "error"]


@dataclass(frozen=True)
class SourceSpec:
    """One row of the exhaustive artifact registry.

    Every path Unifideck can read or write has a row here, including
    the ones we never ship. Rows we don't collect still get audited
    for presence, which is how "your GOG token is missing" becomes a
    one-line answer instead of a round-trip.
    """

    key: str
    what: str
    root: Root
    pattern: str
    arch_dir: str = ""
    policy: Policy = "include"
    max_bytes: int = CAP_DEFAULT
    scrub: Scrub = "text"
    priority: int = 50
    newest_n: int = 0
    expect: Expect = "optional"
    writer: str = ""
    note: str = ""
    # Names matching this glob are dropped after expansion. Exists
    # for one real case: the launch archive keeps ``<id>.log`` and
    # ``<id>.game.log`` side by side, and ``*.log`` matches both.
    exclude: str = ""

    @property
    def collected(self) -> bool:
        """True when this row contributes file bytes to the archive."""
        return self.policy in ("include", "tail")


@dataclass
class PathRecord:
    """Audit row: where a thing should be, and whether it is."""

    key: str
    what: str
    expected_path: str
    resolved_via: str
    status: str
    expect: Expect
    action: str
    size: int | None = None
    mtime: float | None = None
    mode: str = ""
    entries: int | None = None
    writer: str = ""
    note: str = ""


@dataclass
class EntryRecord:
    """One file actually written into the archive."""

    key: str
    arch_path: str
    source_path: str
    source_name: str
    bytes_source: int
    bytes_written: int
    truncated: bool
    mtime: float | None
    scrub: Scrub
    redactions: int = 0
    lines_dropped: int = 0


@dataclass
class SkipRecord:
    """A source deliberately or unavoidably not collected."""

    key: str
    reason: str
    path: str = ""
    size_bytes: int | None = None
    note: str = ""


@dataclass
class CheckResult:
    """Verdict of one derived sanity check."""

    name: str
    status: CheckStatus
    detail: str = ""


@dataclass
class BundleContext:
    """Everything the collector and probes need, resolved once.

    ``roots`` maps each :data:`Root` to the resolved directory (or
    ``None`` when resolution failed) plus the label of the fallback
    rung that won — the label is what makes "why did it look there"
    answerable from the bundle alone.
    """

    roots: dict[str, str | None] = field(default_factory=dict)
    root_sources: dict[str, str] = field(default_factory=dict)
    dest_dir: str = ""
    dest_source: str = ""
    dest_candidates: list[str] = field(default_factory=list)
    config: Any = None
    paths: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def root(self, name: str) -> str | None:
        """Return the resolved directory for ``name``, if any."""
        return self.roots.get(name)
