"""The shared Xbox PC on-disk layout: paths, markers, locks, records.

Every test points ``XDG_DATA_HOME`` at a temporary folder, so nothing here
can touch the user's ``~/.local/share/unifideck``.
"""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from unifideck.launcher.xbox_pc import backend, component_locks, install_records, layout
from unifideck.launcher.xbox_pc.component_locks import (
    ComponentsBusy,
    components_in_use,
    game_lock,
    gc_unreferenced,
    reading_components,
    sweep_staging,
)
from unifideck.launcher.xbox_pc.layout import ActiveComponents

PID = "9PK087LNGJC5"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    backend._DIGEST_CACHE.clear()
    return tmp_path / "data" / "unifideck" / "xbox_pc"


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _install_backend(name: str = "0.1.0-b2b95ee8") -> tuple[Path, str]:
    """A backend folder with a manifest. Returns (dir, manifest sha256)."""
    directory = layout.backend_root() / name
    directory.mkdir(parents=True)
    files = {}
    for fname, body in (("xodus-cli", b"cli"), ("xodus-service", b"svc"), ("LICENSE", b"MIT")):
        (directory / fname).write_bytes(body)
        files[fname] = hashlib.sha256(body).hexdigest()
    for exe in ("xodus-cli", "xodus-service"):
        (directory / exe).chmod(0o755)
    raw = json.dumps({"schema": 1, "version": "0.1.0", "files": files}).encode()
    (directory / "manifest.json").write_bytes(raw)
    return directory, hashlib.sha256(raw).hexdigest()


def _active(root: Path, backend_name: str = "0.1.0-b2b95ee8", runtime: str = "0.1.0") -> ActiveComponents:
    layout.ensure_private_dir(root)
    bdir, sha = _install_backend(backend_name)
    rdir = layout.runtime_root() / runtime
    rdir.mkdir(parents=True)
    return ActiveComponents(
        backend_version="0.1.0", backend_dir=bdir, manifest_sha256=sha,
        runtime_version=runtime, runtime_dir=rdir, archive_sha512="ab" * 64,
        features=("cloud-saves-pr28",), degraded=("xbox_sign_in_in_game",),
        source="/src", installed_at=1.5,
    )


# ── paths ────────────────────────────────────────────────────────────────

def test_data_root_honours_absolute_xdg_data_home(root: Path) -> None:
    assert layout.data_root() == root
    assert layout.credential_dir() == root / "credentials"
    assert layout.installed_dir() == root / "installed"
    assert layout.account_state_path() == root / "state" / "account.json"


def test_relative_xdg_data_home_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "relative/dir")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert layout.data_root() == tmp_path / ".local" / "share" / "unifideck" / "xbox_pc"


def test_install_root_layout() -> None:
    root = layout.install_root_for("/mnt/sd", PID)
    assert root == Path("/mnt/sd/Xbox") / PID
    assert layout.game_dir_for(root) == root / "game"


@pytest.mark.parametrize("bad", ["9pk087lngjc5", "../../etc/xx", "9PK087LNGJC", "", "9PK087LNGJC5\n"])
def test_bad_product_ids_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        layout.install_root_for("/mnt", bad)


def test_xodus_store_membership() -> None:
    assert layout.is_xodus_store("microsoft")
    assert not layout.is_xodus_store("epic")
    assert layout.XBOX_PC_ENABLED_KEY == "stores.microsoft.xbox_pc.enabled"


# ── private files ────────────────────────────────────────────────────────

def test_ensure_private_dir_creates_and_tightens(tmp_path: Path) -> None:
    target = tmp_path / "a" / "priv"
    layout.ensure_private_dir(target)
    assert _mode(target) == 0o700
    target.chmod(0o755)
    layout.ensure_private_dir(target)
    assert _mode(target) == 0o700


def test_ensure_private_dir_refuses_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(layout.UnsafePathError):
        layout.ensure_private_dir(link)


def test_write_private_json_is_0600_and_leaves_no_temp(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    layout.write_private_json(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert _mode(path) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


# ── active.json ──────────────────────────────────────────────────────────

def test_active_round_trip_stores_relative_dirs(root: Path) -> None:
    active = _active(root)
    layout.write_active(active)
    raw = json.loads(layout.active_path().read_text())
    assert raw["backend"]["dir"] == "backend/0.1.0-b2b95ee8"
    assert raw["runtime"]["dir"] == "runtime/0.1.0"
    assert _mode(layout.active_path()) == 0o600
    assert layout.read_active() == active


def test_corrupt_or_escaping_marker_reads_as_none(root: Path) -> None:
    active = _active(root)
    layout.write_active(active)
    raw = json.loads(layout.active_path().read_text())
    raw["runtime"]["dir"] = "runtime/../../etc"
    layout.active_path().write_text(json.dumps(raw))
    assert layout.read_active() is None
    layout.active_path().write_text("{not json")
    assert layout.read_active() is None


def test_marker_naming_a_deleted_folder_reads_as_none(root: Path) -> None:
    active = _active(root)
    layout.write_active(active)
    active.runtime_dir.rmdir()
    assert layout.read_active() is None


# ── backend verification ────────────────────────────────────────────────

def test_resolve_backend_verifies_hashes_and_exec_bits(root: Path) -> None:
    active = _active(root)
    paths = backend.resolve_backend(active)
    assert paths is not None
    assert paths.cli == active.backend_dir / "xodus-cli"
    assert paths.service == active.backend_dir / "xodus-service"


def test_offline_licences_come_only_from_a_verified_manifest_patch_list(root: Path) -> None:
    active = _active(root)
    paths = backend.resolve_backend(active)
    assert paths is not None and not paths.offline_licences
    manifest = active.backend_dir / "manifest.json"
    data = json.loads(manifest.read_bytes())
    data["patches"] = ["xodus-linux-run.patch", backend.OFFLINE_LICENCE_PATCH]
    raw = json.dumps(data).encode()
    manifest.write_bytes(raw)
    upgraded = dataclasses.replace(active, manifest_sha256=hashlib.sha256(raw).hexdigest())
    paths = backend.resolve_backend(upgraded)
    assert paths is not None and paths.offline_licences
    # The same list under the old manifest hash is a tampered manifest.
    assert backend.check_backend(active) == (None, "backend_tampered")


def test_tampered_binary_is_refused(root: Path) -> None:
    active = _active(root)
    assert backend.resolve_backend(active) is not None
    (active.backend_dir / "xodus-cli").write_bytes(b"evil!")
    assert backend.check_backend(active) == (None, "backend_tampered")


def test_missing_exec_bit_is_refused(root: Path) -> None:
    active = _active(root)
    (active.backend_dir / "xodus-service").chmod(0o644)
    assert backend.check_backend(active) == (None, "backend_tampered")


def test_rewritten_manifest_is_refused(root: Path) -> None:
    active = _active(root)
    (active.backend_dir / "manifest.json").write_text('{"files": {}}')
    assert backend.check_backend(active) == (None, "backend_tampered")


def test_missing_binary_is_reported_as_missing(root: Path) -> None:
    active = _active(root)
    (active.backend_dir / "LICENSE").unlink()
    assert backend.check_backend(active) == (None, "backend_missing")


def test_a_cli_on_path_is_never_used(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "bin" / "xodus-cli"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake.parent))
    assert backend.resolve_backend(None) is None


def test_sha512_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x" * (3 << 20))
    assert backend.sha512_file(path) == hashlib.sha512(path.read_bytes()).hexdigest()


# ── locks ────────────────────────────────────────────────────────────────

def _hold(path: Path, flag: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, flag | fcntl.LOCK_NB)
    return fd


def test_reading_components_with_nothing_installed(root: Path) -> None:
    with reading_components() as active:
        assert active is None


def test_reading_components_holds_both_session_locks(root: Path) -> None:
    active = _active(root)
    layout.write_active(active)
    with reading_components() as held:
        assert held == active
        assert components_in_use() is True
        assert not component_locks.try_exclusive(
            component_locks.runtime_lock_path(active.runtime_dir.name),
        )
    assert components_in_use() is False


def test_reading_components_is_busy_while_install_lock_is_exclusive(root: Path) -> None:
    layout.write_active(_active(root))
    fd = _hold(layout.install_lock_path(), fcntl.LOCK_EX)
    try:
        with pytest.raises(ComponentsBusy):
            with reading_components(timeout_s=0.2):
                pytest.fail("must not enter")
    finally:
        os.close(fd)


def test_gc_keeps_held_and_active_folders(root: Path) -> None:
    old = _active(root, backend_name="0.0.9-aaaaaaaa", runtime="0.0.9")
    new = ActiveComponents(**{
        **old.__dict__,
        "backend_dir": _install_backend("0.1.0-bbbbbbbb")[0],
        "runtime_dir": layout.runtime_root() / "0.1.0",
    })
    new.runtime_dir.mkdir()
    (layout.runtime_root() / "0.0.8").mkdir()
    fd = _hold(component_locks.runtime_lock_path("0.0.9"), fcntl.LOCK_SH)
    try:
        removed = gc_unreferenced(new)
    finally:
        os.close(fd)
    assert removed == ["backend/0.0.9-aaaaaaaa", "runtime/0.0.8"]
    assert old.runtime_dir.is_dir()
    assert new.backend_dir.is_dir() and new.runtime_dir.is_dir()
    assert gc_unreferenced(new) == ["runtime/0.0.9"]


def test_gc_unlinks_a_symlink_without_following_it(root: Path, tmp_path: Path) -> None:
    active = _active(root)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    (layout.runtime_root() / "evil").symlink_to(victim)
    assert gc_unreferenced(active) == ["runtime/evil"]
    assert (victim / "keep").exists()


def test_sweep_staging_keeps_the_current_one(root: Path) -> None:
    layout.ensure_private_dir(root)
    stale = root / ".staging-old"
    mine = root / ".staging-mine"
    stale.mkdir()
    mine.mkdir()
    (stale / "big").write_text("x")
    assert sweep_staging(keep=mine) == [".staging-old"]
    assert mine.is_dir() and not stale.exists()


def test_gc_deletes_a_read_only_folder(root: Path) -> None:
    """Archives can hold 0555 folders. GC makes them writable and goes on."""
    active = _active(root)
    old = layout.runtime_root() / "0.0.7"
    (old / "files/share").mkdir(parents=True)
    (old / "files/share/data").write_text("x")
    (old / "files/share").chmod(0o555)
    assert gc_unreferenced(active) == ["runtime/0.0.7"]
    assert not old.exists()


def test_gc_and_staging_failures_never_raise(
    root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup runs after the promote. A folder that resists is kept and logged."""
    active = _active(root)
    stuck = layout.runtime_root() / "0.0.7"
    stuck.mkdir()
    stale = root / ".staging-old"
    stale.mkdir()

    def refuse(_path: Path) -> None:
        raise PermissionError("busy")

    monkeypatch.setattr(component_locks, "_remove_tree", refuse)
    assert gc_unreferenced(active) == []
    assert sweep_staging() == []
    assert stuck.is_dir() and stale.is_dir()


def test_game_lock_is_exclusive_per_game(root: Path) -> None:
    with game_lock(PID) as first:
        assert first is True
        with game_lock(PID) as second:
            assert second is False
        with game_lock("9NBLGGH4R315") as other:
            assert other is True
        assert components_in_use() is True
    assert _mode(layout.locks_dir()) == 0o700


def test_game_lock_refuses_a_bad_id(root: Path) -> None:
    with pytest.raises(ValueError):
        with game_lock("../x"):
            pass


# ── install records ──────────────────────────────────────────────────────

def _record(state: str = "installed", pid: str = PID) -> dict[str, object]:
    return {
        "schema": 1, "productId": pid, "state": state, "market": "IN",
        "contentId": "1760268b-59d2-488b-8f68-b864a82b23be",
        "installRoot": f"/mnt/Xbox/{pid}", "gameDir": f"/mnt/Xbox/{pid}/game",
        "exe": f"/mnt/Xbox/{pid}/game/Balatro.exe", "exeRelative": "Balatro.exe",
    }


def test_record_round_trip(root: Path) -> None:
    path = install_records.write_install_record(_record())
    assert path == root / "installed" / f"{PID}.json"
    assert _mode(path) == 0o600
    assert _mode(root / "installed") == 0o700
    assert install_records.read_install_record(PID) == _record()


@pytest.mark.parametrize("change", [
    {"schema": 2}, {"state": "done"}, {"productId": "../x"},
])
def test_invalid_records_are_never_written(root: Path, change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        install_records.write_install_record({**_record(), **change})


def test_read_rejects_mismatched_and_bad_ids(root: Path) -> None:
    install_records.write_install_record(_record())
    path = root / "installed" / f"{PID}.json"
    path.write_text(json.dumps({**_record(), "productId": "9NBLGGH4R315"}))
    assert install_records.read_install_record(PID) is None
    assert install_records.read_install_record("not-an-id") is None


def test_list_reports_unreadable_files_without_deleting_them(root: Path) -> None:
    install_records.write_install_record(_record("partial"))
    install_records.write_install_record(_record("installed", "9NBLGGH4R315"))
    folder = root / "installed"
    (folder / "9ZZZZZZZZZZZ.json").write_text("{broken")
    (folder / "notes.json").write_text("{}")
    (folder / f".{PID}.json.abc.tmp").write_text("{}")
    records, unreadable = install_records.list_install_records()
    assert sorted(r["productId"] for r in records) == ["9NBLGGH4R315", PID]
    assert sorted(p.name for p in unreadable) == ["9ZZZZZZZZZZZ.json", "notes.json"]
    assert (folder / "9ZZZZZZZZZZZ.json").exists()


def test_list_with_no_folder_is_empty(root: Path) -> None:
    assert install_records.list_install_records() == ([], [])


def test_remove_record_is_idempotent(root: Path) -> None:
    install_records.write_install_record(_record())
    install_records.remove_install_record(PID)
    install_records.remove_install_record(PID)
    assert install_records.read_install_record(PID) is None
