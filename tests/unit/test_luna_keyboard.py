"""Unit tests for the Luna virtual-keyboard script builder (task 7.4, Req 7)."""
from __future__ import annotations

import pytest

from unifideck.stores.luna.luna_keyboard import (
    build_virtual_keyboard_script,
    select_layout,
)


@pytest.mark.parametrize(("locale", "expected"), [
    ("fr", "azerty"),
    ("fr-FR", "azerty"),
    ("FR-fr", "azerty"),
    ("en-US", "qwerty"),
    ("de-DE", "qwerty"),
    ("", "qwerty"),
    (None, "qwerty"),
])
def test_select_layout(locale: str | None, expected: str) -> None:
    assert select_layout(locale) == expected


def test_script_embeds_selected_layout_and_focus_wiring() -> None:
    azerty = build_virtual_keyboard_script("fr-FR")
    assert '"azerty"' in azerty
    # AZERTY top letter row present (serialised per-key), QWERTY one absent.
    assert '["a", "z", "e", "r", "t", "y"' in azerty
    assert '["q", "w", "e", "r", "t", "y"' not in azerty
    # Focus/blur wiring and idempotence guard are present (Req 7.1, 7.2).
    assert "focusin" in azerty and "focusout" in azerty
    assert "__unifideckLunaKeyboard" in azerty


def test_default_locale_builds_qwerty() -> None:
    script = build_virtual_keyboard_script(None)
    assert '"qwerty"' in script
    assert '["q", "w", "e", "r", "t", "y"' in script
