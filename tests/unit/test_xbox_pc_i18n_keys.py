"""Every Xbox PC string the backend names must exist in every locale.

The launcher and the PC library send i18n keys, not text, and they build
the keys in code (``REASON_TOAST_KEYS``, ``toast_header``, the
``SYNC_SKIPPED`` reason map), so ``check_orphan_keys`` cannot see them. A key
missing from a locale shows the raw key to the user instead of a sentence.
This pins the backend keys against ``en-US`` and against every other locale.
Their length is ``scripts/check_toast_length.py``'s job.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher.xbox_pc import errors
from unifideck.stores.microsoft.pc_library.library import SYNC_SKIPPED_REASONS

ROOT = Path(__file__).resolve().parents[2]
LOCALES = ROOT / "src" / "i18n" / "locales"
LISTENER = ROOT / "src" / "services" / "boot-event-listener.tsx"


def _launcher_keys() -> set[str]:
    return {
        *errors.REASON_TOAST_KEYS.values(), errors.LAUNCH_FAILED_KEY, errors.STARTING_KEY,
        errors.LANGUAGE_UNAVAILABLE_KEY, errors.GAME_ARGS_IGNORED_KEY,
        errors.VC_RUNTIME_INSTALLING_KEY, errors.VC_RUNTIME_FAILED_KEY, errors.INITIALIZING_PREFIX_KEY,
        errors.INSTALL_FROM_STEAM_KEY, "toasts.header.xboxPc", "toasts.header.gameSetup",
    }


def _sync_skipped_keys() -> dict[str, str]:
    """``reason -> key`` for the Xbox PC reasons, read from the listener's map."""
    source = LISTENER.read_text(encoding="utf-8")
    found = dict(re.findall(r'^\s*(xbox_pc_[a-z_]+):\s*"([^"]+)"', source, re.MULTILINE))
    return {reason: found.get(reason, "") for reason in SYNC_SKIPPED_REASONS}


def _lookup(tree: dict[str, Any], key: str) -> Any:
    node: Any = tree
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _locale(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((LOCALES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def _all_keys() -> set[str]:
    return _launcher_keys() | set(_sync_skipped_keys().values()) | {"toasts.header.xboxLibrary"}


def test_every_xbox_pc_sync_reason_has_a_toast_row() -> None:
    missing = [reason for reason, key in _sync_skipped_keys().items() if not key]
    assert missing == []


def test_every_backend_key_is_in_english() -> None:
    en = _locale("en-US")
    missing = sorted(k for k in _all_keys() if not isinstance(_lookup(en, k), str))
    assert missing == []


@pytest.mark.parametrize(
    "locale", sorted(p.stem for p in LOCALES.glob("*.json") if p.stem != "en-US"),
)
def test_every_backend_key_is_translated(locale: str) -> None:
    data = _locale(locale)
    missing = sorted(k for k in _all_keys() if not isinstance(_lookup(data, k), str))
    assert missing == [], f"{locale} lacks {len(missing)} Xbox PC key(s)"
