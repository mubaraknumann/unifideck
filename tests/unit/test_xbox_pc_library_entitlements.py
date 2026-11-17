"""``XboxPcLibrary.entitlements``: the entitlement list the game page's DLC list reads.

It must not wait for a sync: it reuses a read younger than ``max_age_s``
(a sync's read included) and otherwise reads again, one read at a time.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit import test_xbox_pc_library as base
from unifideck.stores.microsoft.pc_library.library import XboxPcLibrary

NOW = base.NOW


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return SimpleNamespace(cache=base._Cache(), bus=base._Bus(), facade=base._Facade(),
                           cli=base._Cli(), catalog=base._Catalog())


class _Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _lib(env: SimpleNamespace, mono: _Mono) -> XboxPcLibrary:
    return XboxPcLibrary(None, env.facade, env.cli, cache=env.cache, bus=env.bus,
                         fetch_products=env.catalog, clock=lambda: NOW, monotonic=mono)


async def test_a_fresh_read_is_reused_until_it_is_old(env: SimpleNamespace) -> None:
    mono = _Mono()
    lib = _lib(env, mono)
    first = await lib.entitlements("IN", max_age_s=300)
    assert first is not None and first.entries and env.cli.calls == 1
    mono.t += 299
    assert await lib.entitlements("IN", max_age_s=300) is first
    assert env.cli.calls == 1
    mono.t += 2
    assert await lib.entitlements("IN", max_age_s=300) is not None
    assert env.cli.calls == 2


async def test_force_reads_again(env: SimpleNamespace) -> None:
    lib = _lib(env, _Mono())
    await lib.entitlements("IN", max_age_s=300)
    await lib.entitlements("IN", max_age_s=300, force=True)
    assert env.cli.calls == 2


async def test_a_sync_read_is_shared(env: SimpleNamespace) -> None:
    lib = _lib(env, _Mono())
    await lib.compose([base._xcloud("9pk087lngjc5", "Balatro")], market="IN")
    calls = env.cli.calls
    assert await lib.entitlements("IN", max_age_s=300) is not None
    assert env.cli.calls == calls


async def test_a_busy_cli_answers_with_the_last_read(env: SimpleNamespace) -> None:
    lib = _lib(env, _Mono())
    first = await lib.entitlements("IN", max_age_s=300)
    env.cli.status = "busy"
    assert await lib.entitlements("IN", max_age_s=300, force=True) is first


async def test_no_read_at_all_is_none(env: SimpleNamespace) -> None:
    env.cli.status = "signed_out"
    assert await _lib(env, _Mono()).entitlements("IN", max_age_s=300) is None
    env.cli.status, env.cli.stdout = "ok", "not json"
    assert await _lib(env, _Mono()).entitlements("IN", max_age_s=300) is None


async def test_feature_off_never_reads(env: SimpleNamespace) -> None:
    env.facade.on = False
    assert await _lib(env, _Mono()).entitlements("IN", max_age_s=300) is None
    assert env.cli.calls == 0


async def test_concurrent_page_loads_share_one_read(env: SimpleNamespace) -> None:
    lib = _lib(env, _Mono())
    a, b = await asyncio.gather(
        lib.entitlements("IN", max_age_s=300), lib.entitlements("IN", max_age_s=300),
    )
    assert a is b and env.cli.calls == 1


async def test_known_kinds_come_from_cached_plans(env: SimpleNamespace) -> None:
    lib = _lib(env, _Mono())
    await lib.compose([], market="IN")
    kinds = lib.known_kinds("IN")
    assert kinds.get("9PK087LNGJC5") == "Game"


async def test_a_busy_first_read_answers_at_once(env: SimpleNamespace) -> None:
    """At boot a sync holds the sign-in: never wait on it (an install is waiting)."""
    env.cli.status = "busy"
    lib = _lib(env, _Mono())
    assert await asyncio.wait_for(lib.entitlements("IN", max_age_s=300), 1) is None
    assert await lib.owned_ids("IN", max_age_s=300) is None


async def test_owned_ids_survive_a_restart_while_busy(env: SimpleNamespace) -> None:
    first = _lib(env, _Mono())
    ids = await first.owned_ids("IN", max_age_s=300)
    assert ids, "the fixture account owns titles"
    env.cli.status = "busy"
    restarted = _lib(env, _Mono())  # same cache, nothing read in memory yet
    assert await restarted.owned_ids("IN", max_age_s=300) == ids


async def test_a_stale_answer_comes_back_at_once_and_refreshes_behind(env: SimpleNamespace) -> None:
    """Install never waits on the CLI when anything is known."""
    mono = _Mono()
    lib = _lib(env, mono)
    first = await lib.owned_ids("IN", max_age_s=300)
    assert env.cli.calls == 1
    mono.t += 301
    env.cli.hang = True  # a slow CLI must not hold the answer
    again = await asyncio.wait_for(lib.owned_ids("IN", max_age_s=300), 1)
    assert again == first
    await asyncio.sleep(0)
    assert env.cli.calls == 2, "one background read started"
    await asyncio.wait_for(lib.owned_ids("IN", max_age_s=300), 1)
    assert env.cli.calls == 2, "not a second one while it runs"
    lib._ent._refresh.cancel()  # type: ignore[union-attr]


async def test_remembered_ids_answer_at_once_after_a_restart(env: SimpleNamespace) -> None:
    ids = await _lib(env, _Mono()).owned_ids("IN", max_age_s=300)
    env.cli.hang = True
    restarted = _lib(env, _Mono())
    assert await asyncio.wait_for(restarted.owned_ids("IN", max_age_s=300), 1) == ids
    restarted._ent._refresh.cancel()  # type: ignore[union-attr]
