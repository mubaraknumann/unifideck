"""Every Xbox PC string the backend names must exist in every locale.

The launcher and the PC library send i18n keys, not text, and they build
the keys in code (``REASON_TOAST_KEYS``, the ``SYNC_SKIPPED`` reason map),
so ``check_orphan_keys`` cannot see them. A key missing from a locale shows
the raw key to the user instead of a sentence. This pins the backend keys
against ``en-US`` and against every other locale, and checks that each
launcher string keeps the placeholders the launcher fills in.
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
    return _launcher_keys() | set(_sync_skipped_keys().values())


def test_every_xbox_pc_sync_reason_has_a_toast_row() -> None:
    missing = [reason for reason, key in _sync_skipped_keys().items() if not key]
    assert missing == []


def test_every_backend_key_is_in_english() -> None:
    en = _locale("en-US")
    missing = sorted(k for k in _all_keys() if not isinstance(_lookup(en, k), str))
    assert missing == []


#: Toasts kept to :data:`_SHORT_MAX` characters so the toast does not cut
#: them off. They leave out the title: the player just pressed Play on it.
_SHORT_KEYS = {
    errors.REASON_TOAST_KEYS["eac_runtime_missing"],
    errors.REASON_TOAST_KEYS["battleye_runtime_missing"],
    errors.REASON_TOAST_KEYS["anticheat_unsupported"],
    errors.REASON_TOAST_KEYS["restart_loop"],
    errors.INSTALL_FROM_STEAM_KEY,
}
_SHORT_MAX = 15


def test_launcher_strings_name_the_game() -> None:
    """``emit_launch_error_toast`` fills ``{{game_key}}`` with the title."""
    en = _locale("en-US")
    for key in _launcher_keys() - _SHORT_KEYS:
        assert "{{game_key}}" in str(_lookup(en, key)), key
    assert "{{reason}}" in str(_lookup(en, errors.LAUNCH_FAILED_KEY))
    assert "{{language}}" in str(_lookup(en, errors.LANGUAGE_UNAVAILABLE_KEY))


@pytest.mark.parametrize("locale", sorted(p.stem for p in LOCALES.glob("*.json")))
def test_short_toasts_fit(locale: str) -> None:
    tree = _locale(locale)
    too_long = {key: _lookup(tree, key) for key in _SHORT_KEYS if len(_lookup(tree, key)) > _SHORT_MAX}
    assert too_long == {}


@pytest.mark.parametrize(
    "locale", sorted(p.stem for p in LOCALES.glob("*.json") if p.stem != "en-US"),
)
def test_every_backend_key_is_translated(locale: str) -> None:
    data = _locale(locale)
    missing = sorted(k for k in _all_keys() if not isinstance(_lookup(data, k), str))
    assert missing == [], f"{locale} lacks {len(missing)} Xbox PC key(s)"


@pytest.mark.parametrize(
    "locale", sorted(p.stem for p in LOCALES.glob("*.json") if p.stem != "en-US"),
)
def test_translations_keep_the_launcher_placeholders(locale: str) -> None:
    data = _locale(locale)
    for key in _launcher_keys() - _SHORT_KEYS:
        value = _lookup(data, key)
        if isinstance(value, str):
            assert "{{game_key}}" in value, f"{locale}: {key}"
