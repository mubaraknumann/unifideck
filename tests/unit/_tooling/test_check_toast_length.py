"""Guard test — the toast length gate measures what the toast can show.

``scripts/check_toast_length.py`` holds every ``toasts.*`` string to the
toast budget (16 characters a field; 22 width units in CJK locales). These
tests pin the rules, so a change to the width model or the placeholder rules
is a deliberate one. The frontend clamp (``src/services/toast-text.ts``)
implements the same model.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_toast_length.py"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_toast_length", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def test_sixteen_characters_fit_and_seventeen_do_not(gate: ModuleType) -> None:
    assert gate.check_value("toasts.a", "Download started", "en-US") == []
    assert gate.check_value("toasts.a", "Download started!", "en-US") != []


def test_a_cluster_with_a_combining_mark_is_one_character(gate: ModuleType) -> None:
    assert gate.width("é" * 16, "fr-FR") == 16


def test_cjk_is_measured_by_width(gate: ModuleType) -> None:
    assert gate.width("ダウンロード開始", "ja-JP") == 8
    assert gate.width("Proton", "ja-JP") == pytest.approx(3.6)
    assert gate.check_value("toasts.a", "あ" * 22, "ja-JP") == []
    assert gate.check_value("toasts.a", "あ" * 23, "ja-JP") != []


def test_an_unbounded_placeholder_must_be_last(gate: ModuleType) -> None:
    assert gate.check_value("toasts.a", "Install: {{gameTitle}}", "en-US") == []
    assert gate.check_value("toasts.a", "{{gameTitle}} done", "en-US") != []


def test_text_before_an_unbounded_placeholder_leaves_room(gate: ModuleType) -> None:
    assert gate.check_value("toasts.a", "0123456789{{gameTitle}}", "en-US") == []
    assert gate.check_value("toasts.a", "01234567890{{gameTitle}}", "en-US") != []


def test_two_unbounded_placeholders_are_refused(gate: ModuleType) -> None:
    assert gate.check_value("toasts.a", "{{name}} {{gameTitle}}", "en-US") != []


def test_a_bounded_placeholder_counts_at_its_maximum(gate: ModuleType) -> None:
    # "Saved (" and ")" are 8 and a size is at most 8.
    assert gate.check_value("toasts.a", "Saved ({{size}})", "en-US") == []
    assert gate.check_value("toasts.a", "Saved now ({{size}})", "en-US") != []


def test_raw_text_placeholders_are_refused(gate: ModuleType) -> None:
    assert gate.check_value("toasts.a", "Failed: {{error}}", "en-US") != []


def test_a_backend_key_outside_the_namespace_is_found(gate: ModuleType) -> None:
    tree = ast.parse(
        'KEY = "errors.thing"\n'
        "launcher_toast(i18n_key=KEY)\n"
        'emit({"i18n_title_key": "toasts.ok"})\n'
        'launcher_toast(i18n_key=f"toasts.launcher.{name}")\n'
    )
    constants = gate._module_constants(tree)
    keys = sorted(gate._resolve(node, constants) for node in gate._key_nodes(tree))
    assert keys == ["errors.thing", "toasts.launcher.", "toasts.ok"]


def _header_problems(gate: ModuleType, source: str) -> list[str | None]:
    tree = ast.parse(source)
    constants = gate._module_constants(tree)
    return [
        gate._header_problem(fields, constants)
        for node in ast.walk(tree)
        if (fields := gate._toast_fields(node))
    ]


def test_a_backend_toast_needs_a_header(gate: ModuleType) -> None:
    assert _header_problems(gate, 'launcher_toast("toasts.a.body")') != [None]
    assert _header_problems(
        gate, 'launcher_toast("toasts.a.body", i18n_title_key="toasts.header.proton")',
    ) == [None]


def test_a_header_is_a_header_key(gate: ModuleType) -> None:
    problems = _header_problems(
        gate, 'emit(i18n_key="toasts.a.body", i18n_title_key="toasts.a.title")',
    )
    assert problems != [None]


def test_a_payload_builder_forwarding_its_caller_is_not_checked(gate: ModuleType) -> None:
    assert _header_problems(gate, 'payload = {"i18n_key": i18n_key}') == [None]
