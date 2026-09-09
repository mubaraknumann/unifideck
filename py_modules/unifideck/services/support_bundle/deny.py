"""support_bundle/deny.py — The never-ship list.

Layer A of the two-layer redaction design, and the one that is an
actual *guarantee* rather than a best effort: these paths are matched
after glob expansion and **before any file is opened**, so a denied
file's contents never enter the process, let alone the archive.

Kept in its own module so the security test has a single import
surface and so adding a pattern is a one-line diff that reviewers
can't miss.

The credential patterns mirror ``_AUTH_DATA_CANDIDATES`` and
``_CONFIG_AUTH_FILES`` in :mod:`unifideck.core.cleanup_sweeps`
— the two lists describe the same set of secrets (that module deletes
them, this one refuses to read them), so they must not drift.

Deny always wins over an include row. A file matched here is recorded
in the manifest as ``denied_secret`` with its size and mtime, because
"your GOG token exists and is 412 bytes" is diagnostically useful
while its contents never are.
"""
from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

# Matched against the file *name* only.
_DENY_NAMES: tuple[str, ...] = (
    # OAuth intermediate state — the URLs carry codes and verifiers.
    "*_auth_url.txt",
    "ubisoft_upc_session.txt",
    # Store credentials. ``user.json`` is legendary's and nile's
    # account file; both live outside our data dir.
    "*token*.json",
    "*tokens*.json",
    "*credential*.json",
    "gogdl_auth.json",
    "gog_credentials.json",
    "user.json",
    # Device identity — not a credential, but it identifies a machine
    # across sessions and buys us nothing diagnostically.
    "device_fingerprint.json",
    # Browser profile leftovers, wherever they turn up.
    "*cookie*",
    "Cookies",
    "Cookies-journal",
    "Login Data",
    "Local State",
    # Key material of any kind.
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    # SQLite sidecars: the -wal can be tens of MB and holds the same
    # rows as the DB we already ship.
    "*.db-wal",
    "*.db-shm",
    # Shipped reference catalogs — a megabyte, identical for everyone.
    "ubisoft_uuid_catalog.json",
    "ubisoft_game_db.txt",
)

# Denied if any of these appears as a path *component*. Globs are
# allowed so ``steamrt*`` covers every umu runtime variant.
_DENY_PARTS: tuple[str, ...] = (
    # Chromium profile: cookies, local storage, saved credentials.
    "edge-auth",
    "chromium-auth",
    # Wine prefixes: tens of gigabytes, and they contain the user's
    # registry and any game-side saved credentials.
    "prefixes",
    # Save data belongs to the user, not in a bug report.
    "saves",
    "save_backups",
    # Heroic's gogdl credential store.
    "heroic_gogdl",
    # umu runtime trees — hundreds of MB of pressure-vessel payload.
    "steamrt*",
)


def is_denied(path: Path) -> tuple[bool, str]:
    """Return ``(denied, matched_pattern)`` for ``path``.

    Checks the filename patterns first (cheapest, and the credential
    case), then the path components. The matched pattern is returned
    so the manifest can record *which* rule fired — a reviewer asking
    "why was this excluded" gets the answer without reading this file.
    """
    name = path.name
    for pattern in _DENY_NAMES:
        if fnmatch(name, pattern):
            return True, pattern
    for part in path.parts:
        for pattern in _DENY_PARTS:
            if fnmatch(part, pattern):
                return True, f"{pattern}/**"
    return False, ""


def deny_patterns() -> list[str]:
    """Return every deny pattern, for the manifest's policy block.

    Shipping the list inside the bundle means a support engineer can
    tell whether an absent file was excluded by policy or genuinely
    missing from the device, without cross-referencing the source.
    """
    return [*_DENY_NAMES, *(f"{p}/**" for p in _DENY_PARTS)]
