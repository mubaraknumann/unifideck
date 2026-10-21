"""Installing the Xodus backend and runtime from a local ``out/`` folder.

The fake source tree uses the real file names, including the ``-source``
decoys that must never be read. The runtime archive is a tiny tar.xz with
the pieces ``validate_runtime`` checks. Everything lives under
``tmp_path``; ``XDG_DATA_HOME`` points there too.
"""
from __future__ import annotations

import asyncio
import tarfile
import threading
from pathlib import Path
from typing import Any

import pytest
from _xodus_backend_fakes import add_member as _add
from _xodus_backend_fakes import make_source
from _xodus_backend_fakes import runtime_archive as _archive

from unifideck.launcher.proton.infrastructure.file_lock import flock_path, try_exclusive
from unifideck.launcher.xbox_pc import backend, layout
from unifideck.launcher.xbox_pc.component_locks import runtime_lock_path
from unifideck.stores.microsoft.xodus import runtime as rt
from unifideck.stores.microsoft.xodus.runtime_source import (
    LocalDirSource,
    SourceError,
    source_from_config,
)


@pytest.fixture(autouse=True)
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(rt, "SPACE_MARGIN_BYTES", 0)
    monkeypatch.setattr(rt, "SPACE_FACTOR", 1)
    backend._DIGEST_CACHE.clear()
    return tmp_path


def _install(out: Path, progress: Any = None, abort: threading.Event | None = None) -> layout.ActiveComponents:
    return rt.install_sync(LocalDirSource(out), progress or (lambda *a: None), abort or threading.Event())


def test_describe_ignores_the_source_decoys(tmp_path: Path) -> None:
    listing = LocalDirSource(make_source(tmp_path)).describe()
    assert listing.runtime.archive.name == "unifideck-xodus-runtime-0.1.0-x86_64.tar.xz"
    assert listing.runtime.top_dir == "unifideck-xodus-runtime-0.1.0"
    assert set(listing.backend.files) == {"xodus-cli", "xodus-service", "LICENSE"}
    assert listing.backend.upstream_version == "0.2.8"


def test_source_errors_name_where_they_looked(tmp_path: Path) -> None:
    with pytest.raises(SourceError) as err:
        LocalDirSource(tmp_path / "nowhere").describe()
    assert err.value.code == f"source_missing:{tmp_path / 'nowhere'}"
    out = make_source(tmp_path)
    (out / "runtime" / "unifideck-xodus-runtime-0.2.0-x86_64.tar.xz").write_bytes(b"x")
    with pytest.raises(SourceError) as err:
        LocalDirSource(out).describe()
    assert err.value.code.startswith("source_ambiguous:")
    with pytest.raises(SourceError) as err:
        source_from_config("https://example.invalid/release")
    assert err.value.code == "source_unsupported"
    assert source_from_config("~/x").root == Path("~/x").expanduser()


def test_success_layout_modes_and_marker(tmp_path: Path) -> None:
    stages: list[str] = []
    active = _install(make_source(tmp_path), lambda stage, done, total: stages.append(stage))
    root = layout.data_root()
    assert (root.stat().st_mode & 0o777) == 0o700
    assert active.runtime_dir == layout.runtime_root() / "0.1.0"
    assert (active.runtime_dir / "files" / "bin" / "msidb").is_symlink()
    assert backend.check_backend(active) == (backend.check_backend(active)[0], None)
    assert layout.read_active() == active
    assert active.degraded == ()
    ordered = [s for i, s in enumerate(stages) if i == 0 or stages[i - 1] != s]
    assert ordered == ["checking", "copying_backend", "verifying", "extracting", "finalizing"]
    assert not list(root.glob(".staging-*"))
    assert not (active.backend_dir / "unifideck-xodus-backend-0.1.0-linux-x86_64-source.tar.xz").exists()


def test_extract_progress_counts_compressed_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt, "_REPORT_EVERY", 1)
    out = make_source(tmp_path)
    size = (out / "runtime" / "unifideck-xodus-runtime-0.1.0-x86_64.tar.xz").stat().st_size
    seen: list[tuple[int, int | None]] = []
    _install(out, lambda stage, done, total: seen.append((done, total)) if stage == "extracting" else None)
    assert all(total == size for _, total in seen)
    assert seen[-1] == (size, size)


def test_no_lock_is_held_while_extracting(tmp_path: Path) -> None:
    free: list[bool] = []

    def probe(stage: str, done: int, total: int | None) -> None:
        if stage == "extracting":
            free.append(try_exclusive(layout.install_lock_path()))

    _install(make_source(tmp_path), probe)
    assert free and all(free)


def test_same_components_again_is_a_no_op(tmp_path: Path) -> None:
    out = make_source(tmp_path)
    first = _install(out)
    assert _install(out) == first


@pytest.mark.parametrize("case, code", [
    ("bad_sum", "runtime_checksum_mismatch"),
    ("traversal", "runtime_invalid:path"),
    ("abs_link", "runtime_invalid:link"),
    ("device", "runtime_invalid:type"),
    ("backend_sha", "backend_checksum_mismatch:xodus-cli"),
    ("incompatible", "runtime_backend_incompatible"),
])
def test_failures_leave_the_old_marker(tmp_path: Path, case: str, code: str) -> None:
    good = _install(make_source(tmp_path))
    out = _bad_source(tmp_path, case)
    with pytest.raises(rt.ComponentInstallError) as err:
        _install(out)
    assert err.value.code.startswith(code)
    assert layout.read_active() == good
    assert not list(layout.data_root().glob(".staging-*"))


def _bad_source(tmp: Path, case: str) -> Path:
    if case == "bad_sum":
        return make_source(tmp, version="0.2.0", bad_sum=True)
    if case == "backend_sha":
        out = make_source(tmp, version="0.2.0")
        (out / "backend" / "xodus-cli").write_bytes(b"swapped after the manifest")
        return out
    if case == "incompatible":
        return make_source(tmp, version="0.2.0", archive=_archive("0.2.0", requires="0.3.0"))
    extras = {
        "traversal": lambda tf, top: _add(tf, f"{top}/../../evil", b"x"),
        "abs_link": lambda tf, top: _add(tf, f"{top}/link", kind=tarfile.SYMTYPE, link="/etc/passwd"),
        "device": lambda tf, top: _add(tf, f"{top}/fifo", kind=tarfile.FIFOTYPE),
    }
    return make_source(tmp, version="0.2.0", archive=_archive("0.2.0", extra=extras[case]))


def test_insufficient_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt, "SPACE_MARGIN_BYTES", 1 << 60)
    with pytest.raises(rt.ComponentInstallError) as err:
        _install(make_source(tmp_path))
    assert err.value.code.startswith("insufficient_space")
    assert layout.read_active() is None


def test_missing_features_are_degraded_not_fatal(tmp_path: Path) -> None:
    out = make_source(tmp_path, archive=_archive("0.1.0", features=["package-locale"]))
    active = _install(out)
    assert active.degraded == ("cloud_saves", "xbox_sign_in_in_game")
    assert layout.read_active() == active


def test_stale_staging_is_swept(tmp_path: Path) -> None:
    layout.ensure_private_dir(layout.data_root())
    stale = layout.data_root() / ".staging-crashed"
    (stale / "runtime").mkdir(parents=True)
    _install(make_source(tmp_path))
    assert not stale.exists()


def test_upgrade_gc_keeps_a_runtime_in_use(tmp_path: Path) -> None:
    first = _install(make_source(tmp_path))
    with flock_path(runtime_lock_path(first.runtime_dir.name), shared=True) as held:
        assert held
        second = _install(make_source(tmp_path, version="0.2.0"))
    assert second.runtime_dir.name == "0.2.0"
    assert first.runtime_dir.is_dir(), "a runtime a launch holds is never deleted"
    assert not first.backend_dir.exists(), "the unused old backend is collected"
    third = _install(make_source(tmp_path, version="0.3.0"))
    assert not first.runtime_dir.exists() and not second.runtime_dir.exists()
    assert third.runtime_dir.is_dir()


def test_cancel_stops_and_cleans_up(tmp_path: Path) -> None:
    abort = threading.Event()

    def progress(stage: str, done: int, total: int | None) -> None:
        if stage == "extracting":
            abort.set()

    with pytest.raises(rt.ComponentInstallError) as err:
        _install(make_source(tmp_path), progress, abort)
    assert err.value.code == "cancelled"
    assert layout.read_active() is None
    assert not list(layout.data_root().glob(".staging-*"))


async def test_concurrent_installs_do_the_work_once(tmp_path: Path) -> None:
    out = make_source(tmp_path)
    calls: list[int] = []

    def source() -> LocalDirSource:
        calls.append(1)
        return LocalDirSource(out)

    installer = rt.ComponentInstaller(source)
    first, second = await asyncio.gather(installer.install(lambda *a: None), installer.install(lambda *a: None))
    assert first == second and len(calls) == 1
    assert not installer.running


async def test_installer_maps_a_bad_source(tmp_path: Path) -> None:
    installer = rt.ComponentInstaller(lambda: source_from_config("https://x.invalid"))
    with pytest.raises(rt.ComponentInstallError) as err:
        await installer.install(lambda *a: None)
    assert err.value.code == "source_unsupported"
    await installer.shutdown()


def test_an_archive_rewritten_after_its_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuild that lands between the hash and the unpack must not be promoted."""
    out = make_source(tmp_path)
    archive = out / "runtime" / "unifideck-xodus-runtime-0.1.0-x86_64.tar.xz"
    real_hash = rt._sha512_with_progress

    def hash_then_rebuild(path: Path, *args: Any) -> str:
        digest = real_hash(path, *args)
        archive.write_bytes(_archive("0.1.0", features=[]))
        return digest

    monkeypatch.setattr(rt, "_sha512_with_progress", hash_then_rebuild)
    with pytest.raises(rt.ComponentInstallError) as err:
        _install(out)
    assert err.value.code == "runtime_checksum_mismatch"
    assert layout.read_active() is None


def _old_tarfile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behave like Python before 3.11.4: no filter, every member trusted."""
    monkeypatch.setattr(rt, "HAS_DATA_FILTER", False)
    monkeypatch.setattr(tarfile.TarFile, "extraction_filter", staticmethod(tarfile.fully_trusted_filter))


def test_the_unfiltered_fallback_refuses_a_symlink_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each link passes the lexical check; together they would reach outside staging."""
    _old_tarfile(monkeypatch)

    def chain(tf: tarfile.TarFile, top: str) -> None:
        _add(tf, f"{top}/a", kind=tarfile.DIRTYPE)
        _add(tf, f"{top}/a/up", kind=tarfile.SYMTYPE, link="..")
        _add(tf, f"{top}/e", kind=tarfile.SYMTYPE, link="a/up/../..")
        _add(tf, f"{top}/e/escaped", b"x")

    out = make_source(tmp_path, archive=_archive("0.1.0", extra=chain))
    with pytest.raises(rt.ComponentInstallError) as err:
        _install(out)
    assert err.value.code.startswith("runtime_invalid:link")
    assert not list(tmp_path.rglob("escaped"))


def test_the_unfiltered_fallback_drops_setuid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _old_tarfile(monkeypatch)

    def setuid(tf: tarfile.TarFile, top: str) -> None:
        _add(tf, f"{top}/files/bin/suid", b"x", mode=0o4777)

    active = _install(make_source(tmp_path, archive=_archive("0.1.0", extra=setuid)))
    mode = (active.runtime_dir / "files/bin/suid").stat().st_mode & 0o7777
    assert mode == 0o755


def test_the_unfiltered_fallback_refuses_a_file_written_through_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A regular file named like an unpacked symlink would be opened through it."""
    _old_tarfile(monkeypatch)

    def overwrite(tf: tarfile.TarFile, top: str) -> None:
        _add(tf, f"{top}/a", kind=tarfile.DIRTYPE)
        _add(tf, f"{top}/a/up", kind=tarfile.SYMTYPE, link="..")
        _add(tf, f"{top}/l", kind=tarfile.SYMTYPE, link="a/up/../../pwned")
        _add(tf, f"{top}/l", b"ESCAPED")

    out = make_source(tmp_path, archive=_archive("0.1.0", extra=overwrite))
    with pytest.raises(rt.ComponentInstallError) as err:
        _install(out)
    assert err.value.code.startswith("runtime_invalid:link")
    assert not list(tmp_path.rglob("pwned"))


def test_the_unfiltered_fallback_refuses_a_symlink_that_resolves_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _old_tarfile(monkeypatch)

    def outside(tf: tarfile.TarFile, top: str) -> None:
        _add(tf, f"{top}/a", kind=tarfile.DIRTYPE)
        _add(tf, f"{top}/a/up", kind=tarfile.SYMTYPE, link="..")
        _add(tf, f"{top}/out", kind=tarfile.SYMTYPE, link="a/up/..")

    out = make_source(tmp_path, archive=_archive("0.1.0", extra=outside))
    with pytest.raises(rt.ComponentInstallError) as err:
        _install(out)
    assert err.value.code == "runtime_invalid:link " + "unifideck-xodus-runtime-0.1.0/out"


def test_the_unfiltered_fallback_keeps_links_inside_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _old_tarfile(monkeypatch)

    def inside(tf: tarfile.TarFile, top: str) -> None:
        _add(tf, f"{top}/files/lib/real.so", b"x")
        _add(tf, f"{top}/files/lib/alias.so", kind=tarfile.SYMTYPE, link="real.so")

    active = _install(make_source(tmp_path, archive=_archive("0.1.0", extra=inside)))
    assert (active.runtime_dir / "files/lib/alias.so").read_bytes() == b"x"
