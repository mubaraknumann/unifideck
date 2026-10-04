"""Drawing the ownership ribbon into a Steam Store page over CDP.

Three properties carry the safety of this feature:

* **exact matching**: the ribbon is drawn only into pages on the requested
  AppID; ``/app/25735`` never matches a page on ``/app/257350``;
* **data, not code**: the payload is a JSON literal and the page builds
  every node with ``textContent``, so a hostile store title cannot execute
  on the logged-in store origin;
* **rule 8**: an unreachable debugger says what it cost and where it
  looked, once.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp
import pytest

from unifideck.cdp import store_ribbon
from unifideck.cdp.store_ribbon import (
    RibbonInjectOutcome,
    inject_store_ribbon,
    matching_store_targets,
)
from unifideck.cdp.store_ribbon_js import _RIBBON_FN_JS, build_ribbon_script

APP = 257350
# Built with chr() so the source stays free of invisible separators (RUF001).
LS = chr(0x2028)
PS = chr(0x2029)


def _target(url: str, kind: str = "page", tid: str = "t1") -> dict[str, Any]:
    return {"id": tid, "type": kind, "url": url, "webSocketDebuggerUrl": f"ws://x/{tid}"}


@pytest.fixture(autouse=True)
def _reset_warned() -> None:
    store_ribbon._warned_ports.clear()


# ── target matching ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "https://store.steampowered.com/app/257350/",
        "https://store.steampowered.com/app/257350/Baldurs_Gate_II_Enhanced_Edition/",
        "https://store.steampowered.com/app/257350?snr=1_5_9__405",
        "https://store.steampowered.com/app/257350#reviews",
        "https://store.steampowered.com/app/257350",
    ],
)
def test_matches_the_app_page(url: str) -> None:
    assert matching_store_targets([_target(url)], APP) == [_target(url)]


@pytest.mark.parametrize(
    "url",
    [
        "https://store.steampowered.com/app/2573500/",  # prefix trap
        "https://store.steampowered.com/app/25735/",
        "https://store.steampowered.com/agecheck/app/257350/",
        "http://store.steampowered.com/app/257350/",
        "https://store.steampowered.com.evil.example/app/257350/",
        "https://evil.example/?u=https://store.steampowered.com/app/257350/",
        "https://store.steampowered.com/",
        "",
    ],
)
def test_rejects_everything_else(url: str) -> None:
    assert matching_store_targets([_target(url)], APP) == []


@pytest.mark.parametrize("kind", ["iframe", "service_worker", "worker", "other"])
def test_only_page_targets_match(kind: str) -> None:
    url = "https://store.steampowered.com/app/257350/"

    assert matching_store_targets([_target(url, kind=kind)], APP) == []


# ── payload embedding ───────────────────────────────────────────────
HOSTILE = {
    "appid": APP,
    "dir": "rtl",
    "overlay": {"tag": "</script><img src=x onerror=alert(1)>", "text": "a'b\"c\\d", "cloud": False},
    "sections": [{
        "kind": "owned",
        "tag": "${alert(1)}",
        "message": "line" + LS + "sep" + PS + "para",
        "chips": [{"label": "中文", "detail": "العربية", "installed": True}],
    }],
    "installed": "`backtick`",
    "via": "via Unifideck",
}


def test_payload_round_trips_as_inert_data() -> None:
    script = build_ribbon_script(HOSTILE)

    assert script.startswith(_RIBBON_FN_JS + "(")
    assert script.endswith(");")
    literal = script[len(_RIBBON_FN_JS) + 1:-2]
    assert json.loads(literal) == HOSTILE


def test_line_separators_never_appear_raw() -> None:
    script = build_ribbon_script(HOSTILE)

    assert LS not in script
    assert PS not in script
    assert script.isascii()


@pytest.mark.parametrize(
    "sink", ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("],
)
def test_the_page_script_never_parses_html_or_code(sink: str) -> None:
    assert sink not in _RIBBON_FN_JS


def test_the_page_script_checks_its_path_before_replacing_an_instance() -> None:
    """A stale evaluation must not tear down a newer, valid ribbon."""
    assert _RIBBON_FN_JS.index("path-mismatch") < _RIBBON_FN_JS.index("prev.teardown")


# ── inject_store_ribbon ─────────────────────────────────────────────
async def test_unreachable_debugger_warns_once_and_names_the_port(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    async def _refused(port: int, **_: Any) -> list[dict[str, Any]]:
        raise aiohttp.ClientConnectionError("refused")

    monkeypatch.setattr(store_ribbon, "list_page_targets", _refused)
    caplog.set_level(logging.WARNING, logger=store_ribbon.__name__)

    first = await inject_store_ribbon(8080, APP, HOSTILE)
    second = await inject_store_ribbon(8080, APP, HOSTILE)

    assert first == second == RibbonInjectOutcome(shown=False, reason="cdp_unavailable")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "http://127.0.0.1:8080/json" in warnings[0]
    assert "cdp.port" in warnings[0]
    assert "ribbon" in warnings[0]


async def test_no_matching_page_after_the_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def _list(port: int, **_: Any) -> list[dict[str, Any]]:
        calls.append(port)
        return [_target("https://store.steampowered.com/app/10/")]

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(store_ribbon, "list_page_targets", _list)
    monkeypatch.setattr(store_ribbon.asyncio, "sleep", _no_sleep)

    outcome = await inject_store_ribbon(8080, APP, HOSTILE, attempts=3)

    assert outcome == RibbonInjectOutcome(shown=False, reason="no_target")
    assert len(calls) == 3


async def test_every_matching_page_is_drawn_into(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://store.steampowered.com/app/257350/"
    drawn: list[str] = []

    async def _list(port: int, **_: Any) -> list[dict[str, Any]]:
        return [
            _target(url, tid="a"),
            _target("https://store.steampowered.com/app/1/", tid="b"),
            _target(url, tid="c"),
        ]

    async def _evaluate(target: dict[str, Any], source: str, **_: Any) -> tuple[bool, Any]:
        assert source == build_ribbon_script(HOSTILE)
        drawn.append(target["id"])
        return True, "installed"

    monkeypatch.setattr(store_ribbon, "list_page_targets", _list)
    monkeypatch.setattr(store_ribbon, "evaluate_in_target", _evaluate)

    outcome = await inject_store_ribbon(8080, APP, HOSTILE)

    assert outcome == RibbonInjectOutcome(shown=True, reason="shown", targets=2)
    assert drawn == ["a", "c"]


async def test_failed_evaluations_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _list(port: int, **_: Any) -> list[dict[str, Any]]:
        return [_target("https://store.steampowered.com/app/257350/")]

    async def _evaluate(target: dict[str, Any], source: str, **_: Any) -> tuple[bool, Any]:
        return False, None

    monkeypatch.setattr(store_ribbon, "list_page_targets", _list)
    monkeypatch.setattr(store_ribbon, "evaluate_in_target", _evaluate)

    outcome = await inject_store_ribbon(8080, APP, HOSTILE)

    assert outcome == RibbonInjectOutcome(shown=False, reason="eval_failed", targets=1)


async def test_a_page_error_is_logged_with_its_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    async def _list(port: int, **_: Any) -> list[dict[str, Any]]:
        return [_target("https://store.steampowered.com/app/257350/")]

    async def _evaluate(target: dict[str, Any], source: str, **_: Any) -> tuple[bool, Any]:
        return True, "error: boom"

    monkeypatch.setattr(store_ribbon, "list_page_targets", _list)
    monkeypatch.setattr(store_ribbon, "evaluate_in_target", _evaluate)
    caplog.set_level(logging.WARNING, logger=store_ribbon.__name__)

    outcome = await inject_store_ribbon(8080, APP, HOSTILE)

    assert outcome == RibbonInjectOutcome(shown=False, reason="eval_failed", targets=1)
    assert any("error: boom" in r.getMessage() for r in caplog.records)


async def test_the_previous_document_is_retried_until_the_new_one_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured on-device: drawing as a page starts loading can reach the
    target while it still holds the previous page (new URL, old document)."""
    answers = ["path-mismatch", "path-mismatch", "installed"]

    async def _list(port: int, **_: Any) -> list[dict[str, Any]]:
        return [_target("https://store.steampowered.com/app/257350/")]

    async def _evaluate(target: dict[str, Any], source: str, **_: Any) -> tuple[bool, Any]:
        return True, answers.pop(0)

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(store_ribbon, "list_page_targets", _list)
    monkeypatch.setattr(store_ribbon, "evaluate_in_target", _evaluate)
    monkeypatch.setattr(store_ribbon.asyncio, "sleep", _no_sleep)

    outcome = await inject_store_ribbon(8080, APP, HOSTILE)

    assert outcome == RibbonInjectOutcome(shown=True, reason="shown", targets=1)
    assert answers == []


async def test_only_stale_documents_give_up_after_the_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _list(port: int, **_: Any) -> list[dict[str, Any]]:
        return [_target("https://store.steampowered.com/app/257350/")]

    async def _evaluate(target: dict[str, Any], source: str, **_: Any) -> tuple[bool, Any]:
        calls.append(target["id"])
        return True, "path-mismatch"

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(store_ribbon, "list_page_targets", _list)
    monkeypatch.setattr(store_ribbon, "evaluate_in_target", _evaluate)
    monkeypatch.setattr(store_ribbon.asyncio, "sleep", _no_sleep)

    outcome = await inject_store_ribbon(8080, APP, HOSTILE, attempts=4)

    assert outcome == RibbonInjectOutcome(shown=False, reason="stale_document", targets=1)
    assert len(calls) == 4


def test_the_page_script_survives_an_early_evaluation() -> None:
    """Drawing as a page starts loading can run before it has an <html>
    element: the observer watches the document itself, and a half-built
    instance is torn down so the next call is not kept as 'unchanged'."""
    assert "observer.observe(document," in _RIBBON_FN_JS
    assert "observe(document.documentElement" not in _RIBBON_FN_JS
    catch = _RIBBON_FN_JS[_RIBBON_FN_JS.rindex("} catch (e) {"):]
    assert "state.teardown()" in catch
