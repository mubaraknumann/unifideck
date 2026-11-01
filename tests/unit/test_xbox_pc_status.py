"""The ``XboxPc`` facade: toggle, components, account and the status event.

Pinned here:

- the status shape is the one the RPCs and the event share, with no
  top-level ``success`` key and no email anywhere;
- turning the feature on needs the notice; turning it off deletes nothing;
- the component install runs in the background and its end, its
  ``degraded`` features and a missing source all show in the status;
- ``status()`` never runs ``profile``; ``refresh_status()`` verifies;
- the event is throttled to 2 per second and carries the status.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from _xodus_backend_fakes import install_backend, make_source, runtime_archive

from unifideck.core.types import Events
from unifideck.launcher.xbox_pc import backend, layout
from unifideck.launcher.xbox_pc.layout import (
    XBOX_PC_ENABLED_KEY,
    XBOX_PC_NOTICE_ACK_KEY,
    XBOX_PC_SOURCE_KEY,
)
from unifideck.rpc.errors import RpcError
from unifideck.rpc.mixins.xbox_pc import XboxPcRPCMixin
from unifideck.stores.microsoft.xodus import runtime as rt
from unifideck.stores.microsoft.xodus import xbox_pc as xp
from unifideck.stores.microsoft.xodus.xbox_pc import XboxPc, XboxPcError, unavailable_status

STATUS_KEYS = {"available", "enabled", "notice_version", "notice_ack_version",
               "xcloud_signed_in", "runtime", "account", "library"}

ACCOUNT_SCRIPT = """
import json, sys
LOG = {log!r}
open(LOG, "a").write(sys.argv[1] + "\\n")
if sys.argv[1] == "account":
    print(json.dumps({{"stored": {stored}, "username": "me@example.com"}}))
elif sys.argv[1] == "profile":
    print(json.dumps({{"schema": 1, "account": "me@example.com", "gamertag": "Gamer"}}))
"""


class FakeConfig:
    def __init__(self, source: str) -> None:
        self.data: dict[str, Any] = {
            XBOX_PC_ENABLED_KEY: False, XBOX_PC_SOURCE_KEY: source, XBOX_PC_NOTICE_ACK_KEY: 0,
        }

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class FakeBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: Any, **kwargs: Any) -> None:
        assert event is Events.XBOX_PC_STATE_CHANGED
        self.events.append(kwargs)


class FakeTokens:
    async def load(self) -> bool:
        return True

    async def build_chain(self) -> Any:
        return type("Chain", (), {"gamertag": "gamer"})()


@pytest.fixture(autouse=True)
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(rt, "SPACE_MARGIN_BYTES", 0)
    monkeypatch.setattr(rt, "SPACE_FACTOR", 1)
    backend._DIGEST_CACHE.clear()
    return tmp_path


def _facade(tmp: Path, source: str | None = None) -> tuple[XboxPc, FakeConfig, FakeBus]:
    config = FakeConfig(source or str(tmp / "missing-out"))
    bus = FakeBus()
    facade = XboxPc(config, lambda: None, FakeTokens(), lambda: False, bus,  # type: ignore[arg-type]
                    xcloud_signed_in=lambda: True)
    return facade, config, bus


def _no_email(status: dict[str, Any]) -> None:
    assert "@" not in json.dumps(status)
    assert "success" not in status


async def test_fresh_status_shape_and_missing_source(tmp_path: Path) -> None:
    facade, _, _ = _facade(tmp_path)
    status = await facade.status()
    assert set(status) == STATUS_KEYS == set(unavailable_status())
    assert status["enabled"] is False and status["xcloud_signed_in"] is True
    assert status["runtime"]["state"] == "missing"
    assert status["runtime"]["error"] == f"source_missing:{tmp_path / 'missing-out'}"
    assert status["account"] == {"state": "unknown"}
    assert status["library"]["installable"] == 0
    _no_email(status)


async def test_turning_on_asks_microsoft_again_instead_of_a_stale_answer(tmp_path: Path) -> None:
    """Turning Xbox PC off resyncs while the Xodus sign-in does not apply, so
    the store's cached answer says "signed out". Turning it back on must ask
    again, or the status says "Microsoft needed" and nothing links (2026-10-31)."""
    signed_in = {"cached": False, "asked": 0}

    async def refresh() -> bool:
        signed_in["asked"] += 1
        signed_in["cached"] = True
        return True

    facade = XboxPc(FakeConfig(str(tmp_path / "missing-out")), lambda: None, FakeTokens(),  # type: ignore[arg-type]
                    lambda: False, FakeBus(),  # type: ignore[arg-type]
                    xcloud_signed_in=lambda: signed_in["cached"], refresh_signed_in=refresh)
    off = await facade.set_enabled(False)
    assert signed_in["asked"] == 0 and off["xcloud_signed_in"] is False
    on = await facade.set_enabled(True, notice_version=xp.NOTICE_VERSION)
    assert signed_in["asked"] == 1
    assert on["xcloud_signed_in"] is True


async def test_enable_needs_the_notice_and_disable_deletes_nothing(tmp_path: Path) -> None:
    facade, config, _ = _facade(tmp_path)
    with pytest.raises(XboxPcError) as err:
        await facade.set_enabled(True)
    assert err.value.code == "notice_not_acknowledged"
    status = await facade.set_enabled(True, notice_version=xp.NOTICE_VERSION)
    assert status["enabled"] is True and config.data[XBOX_PC_NOTICE_ACK_KEY] == xp.NOTICE_VERSION
    cred = layout.credential_dir()
    cred.mkdir(parents=True)
    (cred / "token").write_text("x")
    status = await facade.set_enabled(False)
    assert status["enabled"] is False
    assert (cred / "token").exists()
    assert (await facade.set_enabled(True))["enabled"] is True


async def _wait_installed(facade: XboxPc) -> dict[str, Any]:
    for _ in range(200):
        status = await facade.status()
        if status["runtime"]["state"] != "installing":
            return status
        await asyncio.sleep(0.05)
    raise AssertionError("install did not finish")


async def test_turning_on_installs_missing_components(tmp_path: Path) -> None:
    """A saved Xodus sign-in skips every other path that installs them."""
    facade, _, _ = _facade(tmp_path, str(make_source(tmp_path)))
    await facade.set_enabled(True, notice_version=xp.NOTICE_VERSION)
    for _ in range(200):
        status = await facade.status()
        if status["runtime"]["state"] == "installed":
            break
        await asyncio.sleep(0.05)
    assert status["runtime"]["state"] == "installed" and status["runtime"]["version"] == "0.1.0"


async def test_install_runs_in_background_and_reports_degraded(tmp_path: Path) -> None:
    out = make_source(tmp_path, archive=runtime_archive("0.1.0", features=[]))
    facade, config, bus = _facade(tmp_path, str(out))
    assert await facade.install_components() == {"started": False, "reason": "xbox_pc_disabled"}
    config.data[XBOX_PC_ENABLED_KEY] = True
    assert await facade.install_components() == {"started": True, "reason": None}
    assert (await facade.install_components())["reason"] == "install_in_progress"
    assert (await facade.status())["runtime"]["state"] == "installing"
    status = await _wait_installed(facade)
    assert status["runtime"]["state"] == "installed"
    assert status["runtime"]["version"] == "0.1.0"
    assert status["runtime"]["degraded"] == ["cloud_saves", "xbox_sign_in_in_game"]
    await asyncio.sleep(0.7)
    assert bus.events and set(bus.events[-1]) == STATUS_KEYS
    assert bus.events[-1]["runtime"]["state"] == "installed"
    await facade.shutdown()


async def test_install_failure_and_outdated(tmp_path: Path) -> None:
    out = make_source(tmp_path, bad_sum=True)
    facade, config, _ = _facade(tmp_path, str(out))
    config.data[XBOX_PC_ENABLED_KEY] = True
    await facade.install_components()
    status = await _wait_installed(facade)
    assert status["runtime"]["state"] == "failed"
    assert status["runtime"]["error"] == "runtime_checksum_mismatch"
    make_source(tmp_path)
    await facade.install_components()
    assert (await _wait_installed(facade))["runtime"]["state"] == "installed"
    make_source(tmp_path, version="0.2.0")
    assert (await facade.refresh_status())["runtime"]["state"] == "outdated"
    await facade.shutdown()


async def test_status_never_runs_profile_and_refresh_verifies(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    install_backend(ACCOUNT_SCRIPT.format(log=str(log), stored="True"))
    facade, config, _ = _facade(tmp_path)
    config.data[XBOX_PC_ENABLED_KEY] = True
    for _ in range(3):
        await facade.status()
    assert not log.exists()
    status = await facade.refresh_status()
    assert status["account"] == {"state": "signed_in", "gamertag": "Gamer"}
    calls = log.read_text().split()
    assert calls.count("profile") == 1
    _no_email(status)
    assert await facade.is_ready()
    await facade.refresh_status()
    assert log.read_text().split().count("profile") == 1, "a verified account is not re-verified"


async def test_a_refused_account_is_an_error_until_a_recheck_passes(tmp_path: Path) -> None:
    """Stored, but Xbox Live refused it: installs must not read it as signed in (I-062)."""
    import time

    from unifideck.stores.microsoft.xodus import account_state

    install_backend(ACCOUNT_SCRIPT.format(log=str(tmp_path / "calls.log"), stored="True"))
    facade, config, _ = _facade(tmp_path)
    config.data[XBOX_PC_ENABLED_KEY] = True
    await asyncio.to_thread(
        account_state.write_state, account_state.AccountState(None, time.time(), refused=True),
    )
    assert (await facade.checked_status())["account"] == {"state": "error", "error": "xbox_refused"}
    assert not await facade.is_ready()
    # Fixed on the user's side (for example a device removed): the next refresh sees it.
    assert (await facade.refresh_status())["account"]["state"] == "signed_in"


async def test_signed_out_account_and_sign_out(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    install_backend(ACCOUNT_SCRIPT.format(log=str(log), stored="False"))
    facade, config, _ = _facade(tmp_path)
    config.data[XBOX_PC_ENABLED_KEY] = True
    assert (await facade.refresh_status())["account"]["state"] == "signed_out"
    assert not await facade.is_ready()
    cleared: list[int] = []

    async def listener() -> None:
        cleared.append(1)

    facade.sign_out_listeners.append(listener)
    status = await facade.sign_out()
    assert "logout" in log.read_text().split()
    assert status["account"]["state"] == "signed_out" and cleared == [1]


async def test_sign_out_without_a_backend_removes_the_files(tmp_path: Path) -> None:
    facade, _, _ = _facade(tmp_path)
    layout.ensure_private_dir(layout.data_root())
    cred = layout.ensure_private_dir(layout.credential_dir())
    (cred / "tokens.json").write_text("{}")
    await facade.sign_out()
    assert not (cred / "tokens.json").exists()


async def test_checked_status_asks_about_an_unknown_account_once(tmp_path: Path) -> None:
    """After a restart the install precondition must not read ``unknown`` as signed out."""
    log = tmp_path / "calls.log"
    install_backend(ACCOUNT_SCRIPT.format(log=str(log), stored="True"))
    facade, config, _ = _facade(tmp_path)
    config.data[XBOX_PC_ENABLED_KEY] = True
    assert (await facade.status())["account"]["state"] == "unknown"
    assert (await facade.checked_status())["account"]["state"] in ("signed_in", "unverified")
    await facade.checked_status()
    assert log.read_text().split() == ["account"], "asked once, profile never"


async def test_a_download_holding_the_credentials_defers_checks_and_sign_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unifideck.stores.microsoft.xodus import cli as cli_mod

    monkeypatch.setattr(cli_mod, "CREDENTIAL_WAIT_S", 0.05)
    log = tmp_path / "calls.log"
    install_backend(ACCOUNT_SCRIPT.format(log=str(log), stored="True"))
    facade, config, _ = _facade(tmp_path)
    config.data[XBOX_PC_ENABLED_KEY] = True
    signed_in = (await facade.refresh_status())["account"]["state"]
    cred = layout.credential_dir()
    (cred / "user-session").write_text("{}")
    async with facade.cli.credential_lock:
        assert (await facade.refresh_status())["account"]["state"] == signed_in, "the cache is kept"
        with pytest.raises(XboxPcError) as err:
            await facade.sign_out()
    assert err.value.code == "xodus_busy"
    assert (cred / "user-session").exists(), "a busy sign-out deletes nothing"


SIGNS_IN_SCRIPT = """
import json, os, sys
if sys.argv[1] == "account":
    stored = os.path.exists({flag!r})
    print(json.dumps({{"stored": stored, "username": "me@example.com"}}))
"""


async def test_the_end_of_a_sign_in_is_announced_only_after_the_account_is_re_read(
    tmp_path: Path,
) -> None:
    """The frontend settles on the first status that is not ``signing_in``.

    The cached account is the one from before the sign-in (not stored), so
    announcing the end before the re-read showed a failure for a sign-in
    that worked (device run, 2026-10-21).
    """
    flag = tmp_path / "signed-in"
    install_backend(SIGNS_IN_SCRIPT.format(flag=str(flag)))
    facade, config, bus = _facade(tmp_path)
    config.data[XBOX_PC_ENABLED_KEY] = True
    assert (await facade.refresh_status())["account"]["state"] == "signed_out"
    await asyncio.sleep(0.6)
    bus.events.clear()
    flag.write_text("")  # what `xodus-cli login` leaves behind
    facade._on_sign_in_change()  # the sign-in task has just ended
    assert (await facade.status())["account"]["state"] == "signing_in"
    for _ in range(100):
        if (await facade.status())["account"]["state"] != "signing_in":
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.6)
    states = [event["account"]["state"] for event in bus.events]
    assert states and states[-1] == "unverified"
    assert "signed_out" not in states
    await facade.shutdown()


async def test_start_sign_in_needs_the_toggle(tmp_path: Path) -> None:
    facade, _, _ = _facade(tmp_path)
    assert (await facade.start_sign_in())["reason"] == "xbox_pc_disabled"


async def test_library_counts_and_throttle(tmp_path: Path) -> None:
    facade, _, bus = _facade(tmp_path)
    await facade.status()
    for i in range(10):
        facade.set_library_counts({"installable": i, "bogus": 1})
    await asyncio.sleep(0.05)
    assert len(bus.events) <= 1
    await asyncio.sleep(0.6)
    assert 1 <= len(bus.events) <= 2
    assert bus.events[-1]["library"]["installable"] == 9
    assert "bogus" not in bus.events[-1]["library"]
    await facade.shutdown()


class _Registry:
    def __init__(self, store: Any) -> None:
        self._store = store

    def get(self, name: str) -> Any:
        return self._store if name == "microsoft" else None


class _Rpc(XboxPcRPCMixin):
    def __init__(self, store: Any) -> None:
        self.registry = _Registry(store)


async def test_rpc_proxy(tmp_path: Path) -> None:
    facade, _, _ = _facade(tmp_path)
    store = type("Store", (), {"xbox_pc": facade})()
    rpc = _Rpc(store)
    assert set(await rpc.get_xbox_pc_status()) == STATUS_KEYS
    with pytest.raises(RpcError) as err:
        await rpc.set_xbox_pc_enabled(True)
    assert err.value.code == "notice_not_acknowledged"
    assert (await rpc.set_xbox_pc_enabled(True, xp.NOTICE_VERSION))["enabled"] is True
    assert (await rpc.start_xbox_pc_sign_in())["pending"] is False
    missing = _Rpc(None)
    assert (await missing.get_xbox_pc_status())["available"] is False
    with pytest.raises(RpcError):
        await missing.install_xbox_pc_runtime()


async def test_components_install_and_update_by_themselves_when_microsoft_connects(
    tmp_path: Path,
) -> None:
    """I-029: the frontend's boot step ran before Microsoft was known to be
    signed in, so a newer build never installed. The backend now does it."""
    from unifideck.stores.microsoft.xodus import auto_components

    out = make_source(tmp_path)
    facade, config, _ = _facade(tmp_path, str(out))
    await auto_components.ensure_components(facade)
    assert (await facade.status())["runtime"]["state"] == "missing", "toggle off: nothing"
    config.data[XBOX_PC_ENABLED_KEY] = True
    await auto_components.ensure_components(facade)
    assert (await _wait_installed(facade))["runtime"]["version"] == "0.1.0"
    make_source(tmp_path, version="0.2.0")
    await auto_components.ensure_components(facade)
    assert (await _wait_installed(facade))["runtime"]["version"] == "0.2.0", "outdated: updated"
    await facade.shutdown()


async def test_a_failed_install_is_not_retried_on_every_sign_in_check(tmp_path: Path) -> None:
    from unifideck.stores.microsoft.xodus import auto_components

    out = make_source(tmp_path, bad_sum=True)
    facade, config, _ = _facade(tmp_path, str(out))
    config.data[XBOX_PC_ENABLED_KEY] = True
    await auto_components.ensure_components(facade)
    assert (await _wait_installed(facade))["runtime"]["state"] == "failed"
    started: list[int] = []
    original = facade.install_components

    async def counting() -> dict[str, Any]:
        started.append(1)
        return await original()

    facade.install_components = counting  # type: ignore[method-assign]
    await auto_components.ensure_components(facade)
    assert started == []
    await facade.shutdown()
