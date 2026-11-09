"""Once a sealing backend is installed, a plain sign-in is replaced, not kept.

The device identity must survive: deleting it costs a Microsoft device slot at
the next sign-in (I-045). Everything lives under ``tmp_path``.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.launcher.xbox_pc.backend import SEALED_CREDENTIALS_PATCH, check_backend
from unifideck.launcher.xbox_pc.layout import account_state_path, credential_dir, license_cache_dir
from unifideck.security import SecureTokenStore
from unifideck.security.secure_token_store import SecureTokenStoreError
from unifideck.stores.microsoft.xodus import credential_reset as cr
from unifideck.stores.microsoft.xodus.cli import XodusCliError
from unifideck.stores.microsoft.xodus.xbox_pc import XboxPc


class _Id:
    def read(self) -> str:
        return "00112233445566778899aabbccddeeff"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(cr, "SecureTokenStore", lambda: SecureTokenStore(device_identity=_Id()))
    monkeypatch.setattr(cr, "components_in_use", lambda: False)
    return tmp_path


def _store() -> SecureTokenStore:
    return SecureTokenStore(device_identity=_Id())


def _plain_install() -> None:
    cred, lic = credential_dir(), license_cache_dir()
    cred.mkdir(parents=True, mode=0o700)
    lic.mkdir(parents=True, mode=0o700)
    for name in ("user-session", "nexus-subscription-v2", "dev_license", "device-tokens"):
        (cred / name).write_text(json.dumps({"name": name}))
        (cred / name).chmod(0o600)
    (cred / ".user-session.1.0.tmp").write_text("{}")
    (lic / "aaaa.json").write_text("{}")
    account_state_path().parent.mkdir(parents=True, exist_ok=True)
    account_state_path().write_text("{}")


def _cli(*, seals: bool = True) -> Any:
    async def backend() -> Any:
        return SimpleNamespace(sealed_credentials=seals)

    return SimpleNamespace(backend=backend, credential_lock=asyncio.Lock())


def test_signs_out_and_keeps_the_device_sealed() -> None:
    _plain_install()
    assert cr.clear_plain_files(_store()) is True
    cred = credential_dir()
    assert sorted(p.name for p in cred.iterdir()) == ["dev_license", "device-tokens"]
    for name in ("dev_license", "device-tokens"):
        raw = (cred / name).read_bytes()
        assert raw[:4] == b"UFD1"
        assert json.loads(_store()._decrypt(raw)) == {"name": name}
        assert (cred / name).stat().st_mode & 0o777 == 0o600
    assert list(license_cache_dir().iterdir()) == []
    assert not account_state_path().exists()


def test_a_second_run_leaves_a_new_sealed_sign_in_alone() -> None:
    _plain_install()
    cr.clear_plain_files(_store())
    cred, lic = credential_dir(), license_cache_dir()
    session, licence = _store().encrypt_bytes(b"{}"), _store().encrypt_bytes(b"{}")
    (cred / "user-session").write_bytes(session)
    (lic / "bbbb.json").write_bytes(licence)
    device = (cred / "dev_license").read_bytes()
    assert cr.clear_plain_files(_store()) is False
    assert (cred / "user-session").read_bytes() == session
    assert (lic / "bbbb.json").read_bytes() == licence
    assert (cred / "dev_license").read_bytes() == device


def test_no_key_deletes_nothing() -> None:
    _plain_install()

    class NoId:
        def read(self) -> str:
            from unifideck.security import DeviceIdentityError
            raise DeviceIdentityError("missing")

    with pytest.raises(SecureTokenStoreError):
        cr.clear_plain_files(SecureTokenStore(device_identity=NoId()))
    assert (credential_dir() / "user-session").exists()


async def test_an_older_backend_keeps_the_plain_sign_in() -> None:
    _plain_install()
    assert await cr.run(_cli(seals=False)) == cr.ResetOutcome()
    assert (credential_dir() / "user-session").read_bytes()[:1] == b"{"


async def test_no_backend_changes_nothing() -> None:
    _plain_install()
    cli = _cli()

    async def missing() -> Any:
        raise XodusCliError("backend_missing")

    cli.backend = missing
    assert await cr.run(cli) == cr.ResetOutcome()
    assert (credential_dir() / "user-session").exists()


async def test_waits_while_a_game_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    _plain_install()
    monkeypatch.setattr(cr, "components_in_use", lambda: True)
    assert await cr.run(_cli()) == cr.ResetOutcome()
    assert (credential_dir() / "user-session").exists()


async def test_waits_while_a_download_holds_the_lock() -> None:
    _plain_install()
    cli = _cli()
    async with cli.credential_lock:
        assert await cr.run(cli) == cr.ResetOutcome()
    assert (credential_dir() / "user-session").exists()


async def test_the_facade_signs_out_once_and_tells_the_status() -> None:
    _plain_install()
    told: list[int] = []
    facade = XboxPc.__new__(XboxPc)
    facade.cli = _cli()
    facade._plain_cleared = False
    facade._reset_notice = False
    facade._saved = None
    facade._account_error = None
    facade.sign_out_listeners = [lambda: told.append(1)]
    facade.notify = lambda: None  # type: ignore[method-assign]
    facade._signin = SimpleNamespace(running=False, last_error=None, step=None)
    facade._settling_sign_in = False
    await facade.clear_plain_credentials()
    await facade.clear_plain_credentials()
    assert told == [1]
    assert facade._account_view() == {"state": "signed_out", "reset": True}


def test_the_manifest_names_the_sealing_patch(tmp_path: Path) -> None:
    import hashlib

    folder = tmp_path / "b"
    folder.mkdir()
    files = {}
    for name in ("xodus-cli", "xodus-service"):
        (folder / name).write_bytes(name.encode())
        (folder / name).chmod(0o755)
        files[name] = hashlib.sha256(name.encode()).hexdigest()
    raw = json.dumps({"files": files, "patches": [SEALED_CREDENTIALS_PATCH]}).encode()
    (folder / "manifest.json").write_bytes(raw)
    ref = SimpleNamespace(backend_dir=folder, backend_version="1", manifest_sha256=hashlib.sha256(raw).hexdigest())
    paths, problem = check_backend(ref)
    assert problem is None and paths is not None and paths.sealed_credentials is True
