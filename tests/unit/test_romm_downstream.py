"""RomM is recognised by the shared per-store tables.

Each table below lives somewhere else in the codebase and decides whether a
store gets size lookups, a direct launch target, a cache, a credential-file
cleanup, or its config file excluded from a support bundle. The file also
covers the default config keys and launch-option parsing. A store missing from
one of them fails quietly, and only for that store.
"""
from __future__ import annotations

import json
from pathlib import Path

from unifideck.bootstrap.cache_registry import _STORE_CACHES
from unifideck.rpc.mixins.executable import _DIRECT_LAUNCH_STORES
from unifideck.services.shortcut.launch_options import extract_store_id
from unifideck.services.size_backfill import SIZE_CAPABLE_STORES

ROOT = Path(__file__).resolve().parents[2]


def test_launch_identity_parses() -> None:
    assert extract_store_id("romm:42") == ("romm", "42")


def test_capability_tables_include_romm() -> None:
    assert "romm" in SIZE_CAPABLE_STORES
    assert "romm" in _DIRECT_LAUNCH_STORES
    assert "romm" in _STORE_CACHES


def test_defaults_are_the_three_romm_keys() -> None:
    cfg = json.loads((ROOT / "defaults/config.json").read_text())
    assert set(cfg["stores"]["romm"]) == {"config_file", "default_install_root", "download_dir"}


def test_auth_cleanup_lists_the_romm_config() -> None:
    from unifideck.core.cleanup_sweeps import _AUTH_DATA_CANDIDATES

    assert "~/.local/share/unifideck/romm_config.json" in _AUTH_DATA_CANDIDATES


def test_support_bundle_denies_the_romm_config() -> None:
    from unifideck.services.support_bundle.deny import is_denied

    assert is_denied(Path("/home/deck/.local/share/unifideck/romm_config.json"))[0]
