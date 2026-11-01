"""stores/microsoft/xodus/package_source.py — which product's package a title streams.

Some Store packages are only a stub: its one action opens the Store page of
the product that really runs the game (ISSUES.md I-046). Such a title
streams that other product's package into its own install folder instead.
Everything else stays the title's own: the record, the folder, the prefix,
the launch and the uninstall. Only resolving, streaming and the update
check ask this table.
"""
from __future__ import annotations

#: Stub product id -> the product id whose package it installs.
PACKAGE_SOURCE: dict[str, str] = {
    "9PJ8266BHFWN": "9PGW18NPBZV5",
}


def package_source(pid: str) -> str:
    """The product whose package ``pid`` streams (``pid`` itself by default)."""
    return PACKAGE_SOURCE.get(pid.upper(), pid)
