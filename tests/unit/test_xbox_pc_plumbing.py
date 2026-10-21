"""Small Xbox PC hooks outside ``stores/microsoft/xodus``.

- the XSTS chain carries the classic gamertag the sign-in compares;
- the device-code sign-in reports ``running``;
- "Delete all data" empties the Xodus credential folder and forgets the
  verified gamertag, without touching anything else of Xbox PC;
- the support bundle stats the credentials and never reads them.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from unifideck.core import cleanup_sweeps
from unifideck.services.support_bundle.sources import COLLECTED
from unifideck.services.support_bundle.sources_audit import AUDIT_ONLY
from unifideck.stores.microsoft.microsoft_device_auth import MicrosoftDeviceAuth
from unifideck.stores.microsoft.tokens.xbl_chain import _chain_from_xsts


def test_xsts_chain_carries_the_gamertag() -> None:
    resp = {"Token": "t", "DisplayClaims": {"xui": [{"uhs": "h", "xid": "1", "gtg": "Gamer One"}]}}
    chain = _chain_from_xsts(resp, "http://xboxlive.com", "xbl")
    assert chain is not None and chain.gamertag == "Gamer One"
    no_tag = _chain_from_xsts({"Token": "t", "DisplayClaims": {"xui": [{"uhs": "h"}]}}, "rp", "x")
    assert no_tag is not None and no_tag.gamertag is None


async def test_device_auth_running() -> None:
    auth = MicrosoftDeviceAuth(None, None, None, lambda: None)  # type: ignore[arg-type]
    assert auth.running is False
    auth._task = asyncio.create_task(asyncio.sleep(10))
    assert auth.running is True
    await auth.cancel()
    assert auth.running is False


def test_sweep_auth_data_empties_the_xodus_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    root = home / ".local/share/unifideck/xbox_pc"
    cred = root / "credentials"
    cred.mkdir(parents=True)
    (cred / "xodus-user.json").write_text("{}")
    (cred / "device.json").write_text("{}")
    (root / "state").mkdir()
    (root / "state" / "account.json").write_text("{}")
    (root / "active.json").write_text("{}")
    (root / "installed").mkdir()
    (root / "installed" / "9PK087LNGJC5.json").write_text("{}")
    assert cleanup_sweeps.sweep_auth_data() == 3
    assert cred.is_dir() and not list(cred.iterdir())
    assert not (root / "state" / "account.json").exists()
    assert (root / "active.json").exists() and (root / "installed" / "9PK087LNGJC5.json").exists()


def test_sweep_auth_data_never_follows_a_symlinked_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("x")
    root = home / ".local/share/unifideck/xbox_pc"
    root.mkdir(parents=True)
    (root / "credentials").symlink_to(elsewhere)
    cleanup_sweeps.sweep_auth_data()
    assert (elsewhere / "keep.txt").exists()


def test_support_bundle_rows() -> None:
    audit = {spec.key: spec for spec in AUDIT_ONLY}
    collected = {spec.key: spec for spec in COLLECTED}
    assert audit["xbox_pc_credentials"].policy == "presence_only"
    assert audit["xbox_pc_credentials"].pattern == "xbox_pc/credentials/*"
    assert audit["xbox_pc_account_state"].policy == "presence_only"
    assert audit["xbox_pc_backend"].policy == "bulk"
    assert audit["xbox_pc_runtime"].policy == "bulk"
    assert collected["xbox_pc_active"].pattern == "xbox_pc/active.json"
    assert not any(spec.pattern.startswith("xbox_pc/credentials") for spec in COLLECTED)
