"""Comet's achievement push receiver: config publishing, auth, dispatch."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from unifideck.services.achievements.comet_events import CometEventReceiver

_ITEM = {"achievement_key": "first_blood", "name": "First Blood", "date_unlocked": "2026-11-04T10:00:00+00:00"}


@pytest.fixture
async def receiver(tmp_path: Path):
    """A started receiver writing its config under tmp_path; unlocks captured."""
    got: list[tuple[str, dict[str, Any]]] = []
    arrived = asyncio.Event()

    async def on_unlock(client_id: str, data: dict[str, Any]) -> None:
        got.append((client_id, data))
        arrived.set()

    rec = CometEventReceiver(on_unlock, config_file=tmp_path / "comet" / "config.toml")
    await rec.start()
    yield rec, got, arrived
    await rec.stop()


async def _post(url: str, body: Any) -> int:
    async with aiohttp.ClientSession() as s, s.post(url, json=body) as r:
        return r.status


def test_default_config_path_follows_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    rec = CometEventReceiver(lambda *_: asyncio.sleep(0))
    assert rec._config_file == tmp_path / ".local/share/unifideck/comet-config/comet/config.toml"


async def test_config_publishes_localhost_url_with_token(receiver) -> None:
    rec, _, _ = receiver
    text = rec._config_file.read_text()
    assert "[[custom_event]]" in text
    assert "enabled = true" in text
    assert f'url = "{rec.url}"' in text
    assert rec.url.startswith("http://127.0.0.1:")
    assert "token=" in rec.url


async def test_valid_unlock_is_dispatched(receiver) -> None:
    rec, got, arrived = receiver
    status = await _post(rec.url, {"name": "achievement_unlocked", "client_id": "123", "data": _ITEM})
    assert status == 204
    await asyncio.wait_for(arrived.wait(), timeout=5)
    assert got == [("123", _ITEM)]


async def test_wrong_token_is_refused(receiver) -> None:
    rec, got, _ = receiver
    bad = rec.url.split("?")[0] + "?token=nope"
    status = await _post(bad, {"name": "achievement_unlocked", "client_id": "1", "data": _ITEM})
    assert status == 403
    assert got == []


@pytest.mark.parametrize("body", [
    {"name": "something_else", "client_id": "1", "data": _ITEM},
    {"name": "achievement_unlocked", "client_id": "1", "data": "not a dict"},
])
async def test_other_events_are_ignored(receiver, body: dict[str, Any]) -> None:
    rec, got, _ = receiver
    assert await _post(rec.url, body) == 204
    await asyncio.sleep(0)
    assert got == []


async def test_non_object_body_is_rejected(receiver) -> None:
    rec, got, _ = receiver
    assert await _post(rec.url, [1, 2]) == 400
    assert got == []


async def test_handler_failure_never_reaches_comet(tmp_path: Path) -> None:
    done = asyncio.Event()

    async def boom(_c: str, _d: dict[str, Any]) -> None:
        done.set()
        raise RuntimeError("routing broke")

    rec = CometEventReceiver(boom, config_file=tmp_path / "config.toml")
    await rec.start()
    try:
        status = await _post(rec.url, {"name": "achievement_unlocked", "client_id": "1", "data": _ITEM})
        assert status == 204
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await rec.stop()


async def test_stop_withdraws_config(tmp_path: Path) -> None:
    rec = CometEventReceiver(lambda *_: asyncio.sleep(0), config_file=tmp_path / "c" / "config.toml")
    await rec.start()
    assert rec._config_file.is_file()
    await rec.stop()
    assert not rec._config_file.exists()
    assert not list(rec._config_file.parent.glob("*.tmp"))
