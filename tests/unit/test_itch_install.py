"""Tests for ``stores.itch.install``: Queue/Perform, progress, stall, cancel.

The fake connection reproduces what butler 15.31.0 did on-device: it picks
``<location>/<slug>`` itself, stages under ``<location>/downloads/<id>``,
reports ``Progress {progress, eta, bps}``, and a queued-but-not-performing
install cannot be cancelled (``didCancel: false``).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from unifideck.stores.itch import install as install_mod
from unifideck.stores.itch.butlerd import ButlerdError
from unifideck.stores.itch.install import ItchInstaller, sweep_staging

GAME = {"id": 2119837, "title": "Fear Assessment", "url": "https://x.itch.io/fear-assessment"}
LINUX_UPLOAD = {"id": 8990996, "type": "default", "filename": "linux64.zip", "storage": "hosted",
                "platforms": {"linux": "all"}, "size": 264115691}


class _Conn:
    """One dedicated install connection."""

    def __init__(self, fake: _Daemon, on_notification: Any) -> None:
        self.fake = fake
        self.notify = on_notification
        self.closed = False

    async def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        self.fake.calls.append((method, params or {}))
        return await getattr(self.fake, method.replace(".", "_"))(self, params or {})

    async def close(self) -> None:
        self.closed = True


class _Daemon:
    def __init__(self, root: Path, *, perform: str = "ok", queue: str = "ok",
                 uploads: list[dict[str, Any]] | None = None, caves: list[dict[str, Any]] | None = None,
                 profiles: list[dict[str, Any]] | None = None) -> None:
        self.root = root
        self.perform_mode = perform
        self.queue_mode = queue
        self.uploads = [LINUX_UPLOAD] if uploads is None else uploads
        self.caves = caves or []
        self.profiles = [{"id": 42}] if profiles is None else profiles
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.locations: list[dict[str, Any]] = []

    # shared-connection calls
    async def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        self.calls.append((method, params or {}))
        return await getattr(self, method.replace(".", "_"))(None, params or {})

    async def connect(self, on_notification: Any = None, **_: Any) -> _Conn:
        return _Conn(self, on_notification)

    async def Profile_List(self, _c: Any, _p: dict[str, Any]) -> dict[str, Any]:
        return {"profiles": self.profiles}

    async def Fetch_Game(self, _c: Any, _p: dict[str, Any]) -> dict[str, Any]:
        return {"game": GAME}

    async def Fetch_GameUploads(self, _c: Any, p: dict[str, Any]) -> dict[str, Any]:
        assert p["compatible"] is False and p["fresh"] is True
        return {"uploads": self.uploads}

    async def Fetch_Caves(self, _c: Any, _p: dict[str, Any]) -> dict[str, Any]:
        return {"items": self.caves}

    async def Install_Locations_List(self, _c: Any, _p: dict[str, Any]) -> dict[str, Any]:
        return {"installLocations": self.locations}

    async def Install_Locations_Add(self, _c: Any, p: dict[str, Any]) -> dict[str, Any]:
        loc = {"id": f"loc-{len(self.locations)}", "path": p["path"]}
        self.locations.append(loc)
        return {"installLocation": loc}

    async def Install_Queue(self, conn: _Conn, p: dict[str, Any]) -> dict[str, Any]:
        staging = self.root / "downloads" / "honestly-glowing-mallard"
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "operate-context.json").write_text("{}")
        if self.queue_mode == "hang":
            await asyncio.sleep(3600)
        return {"id": "honestly-glowing-mallard", "caveId": "cave-1",
                "installFolder": str(self.root / "fear-assessment"),
                "stagingFolder": str(staging), "installLocationId": p.get("installLocationId")}

    async def Install_Perform(self, conn: _Conn, p: dict[str, Any]) -> dict[str, Any]:
        folder = self.root / "fear-assessment"
        (folder / "linux64").mkdir(parents=True, exist_ok=True)
        conn.notify("Progress", {"progress": 0.4, "eta": 5, "bps": 20_000_000.0})
        if self.perform_mode in ("hang", "stall"):
            await asyncio.sleep(3600)
        if self.perform_mode == "fail":
            raise ButlerdError("network error while downloading")
        nw = folder / "linux64" / "nw"
        nw.write_bytes(b"\x7fELF" + b"\0" * 64)
        nw.chmod(0o755)
        conn.notify("Progress", {"progress": 1.0, "eta": 0, "bps": 30_000_000.0})
        return {"caveId": "cave-1", "events": []}

    async def Install_Cancel(self, _c: Any, _p: dict[str, Any]) -> dict[str, Any]:
        return {"didCancel": True}

    async def Uninstall_Perform(self, _c: Any, p: dict[str, Any]) -> dict[str, Any]:
        self.caves = [c for c in self.caves if c["id"] != p["caveId"]]
        return {}


@pytest.fixture(autouse=True)
def _no_foreign_installs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_mod, "foreign_installs_under", lambda *_a, **_k: [])


def _installer(daemon: _Daemon, stall_s: float = 900) -> ItchInstaller:
    from unifideck.stores.itch.library import ItchLibraryReader
    return ItchInstaller(daemon, ItchLibraryReader(daemon), default_install_root=str(daemon.root),
                         stall_s=stall_s)


async def test_install_success_reports_native_exe_and_writes_manifest(tmp_path: Path) -> None:
    daemon = _Daemon(tmp_path)
    progress: list[Any] = []

    async def cb(p: Any) -> None:
        progress.append(p)

    result = await _installer(daemon).install("2119837", str(tmp_path), cb)
    folder = tmp_path / "fear-assessment"
    assert result.success, result.error
    assert result.install_path == str(folder)
    assert result.metadata["exe_path"] == str(folder / "linux64" / "nw")
    assert result.metadata["platform"] == "linux"
    # butler leaves an empty <root>/downloads after a successful install.
    assert not (tmp_path / "downloads").exists()
    manifest = json.loads((folder / ".unifideck_manifest.json").read_text())
    assert manifest["store"] == "itch" and manifest["executable_relative"] == "linux64/nw"
    phases = [p.get("phase") for p in progress]
    assert phases[:2] == ["preparing", "downloading"]
    assert any(p.get("percentage") == 100.0 and p.get("speed_bps") for p in progress)
    queue = next(p for m, p in daemon.calls if m == "Install.Queue")
    assert queue["installLocationId"] == "loc-0" and queue["profileId"] == 42


async def test_install_location_is_registered_once(tmp_path: Path) -> None:
    daemon = _Daemon(tmp_path)
    await _installer(daemon).install("2119837", str(tmp_path), None)
    await _installer(daemon).install("2119837", str(tmp_path), None)
    assert [m for m, _ in daemon.calls].count("Install.Locations.Add") == 1


async def test_quiet_install_is_failed_as_stalled_and_cleaned_up(tmp_path: Path) -> None:
    daemon = _Daemon(tmp_path, perform="stall")
    result = await _installer(daemon, stall_s=0.3).install("2119837", str(tmp_path), None)
    assert not result.success
    assert result.error.startswith("stalled:")  # classified as errors.download.stalled
    assert ("Install.Cancel", {"id": "honestly-glowing-mallard"}) in daemon.calls
    assert not (tmp_path / "fear-assessment").exists()
    assert not (tmp_path / "downloads").exists()


async def test_butler_failure_text_reaches_the_error(tmp_path: Path) -> None:
    daemon = _Daemon(tmp_path, perform="fail")
    result = await _installer(daemon).install("2119837", str(tmp_path), None)
    assert not result.success
    assert "network error while downloading" in result.error


async def test_cancel_while_downloading_cancels_butler_and_removes_the_new_folder(tmp_path: Path) -> None:
    daemon = _Daemon(tmp_path, perform="hang")
    task = asyncio.create_task(_installer(daemon).install("2119837", str(tmp_path), None))
    for _ in range(100):
        if (tmp_path / "fear-assessment").exists():
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ("Install.Cancel", {"id": "honestly-glowing-mallard"}) in daemon.calls
    assert not (tmp_path / "fear-assessment").exists()
    assert not (tmp_path / "downloads").exists()


async def test_cancel_while_queueing_sweeps_the_staging_butler_left(tmp_path: Path) -> None:
    daemon = _Daemon(tmp_path, queue="hang")
    task = asyncio.create_task(_installer(daemon).install("2119837", str(tmp_path), None))
    for _ in range(100):
        if (tmp_path / "downloads").exists():
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "downloads").exists()


async def test_a_folder_owned_by_another_store_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install_mod, "foreign_installs_under", lambda *_a, **_k: ["gog:1207658924"])
    daemon = _Daemon(tmp_path)
    (tmp_path / "fear-assessment").mkdir()
    (tmp_path / "fear-assessment" / "gog-file").write_text("keep me")
    result = await _installer(daemon).install("2119837", str(tmp_path), None)
    assert not result.success and result.error.startswith("itch_folder_in_use:")
    assert (tmp_path / "fear-assessment" / "gog-file").read_text() == "keep me"
    assert "Install.Perform" not in [m for m, _ in daemon.calls]


async def test_no_installable_upload_is_a_classified_failure(tmp_path: Path) -> None:
    daemon = _Daemon(tmp_path, uploads=[{"id": 1, "type": "soundtrack", "platforms": {"windows": "all"}}])
    result = await _installer(daemon).install("2119837", str(tmp_path), None)
    assert not result.success and result.error.startswith("itch_no_installable_upload:")


async def test_signed_out_install_asks_for_login(tmp_path: Path) -> None:
    daemon = _Daemon(tmp_path, profiles=[])
    result = await _installer(daemon).install("2119837", str(tmp_path), None)
    assert not result.success and "login" in result.error  # → errors.download.authExpired


async def test_stale_cave_is_dropped_before_installing(tmp_path: Path) -> None:
    stale = {"id": "old-cave", "game": {"id": 2119837, "title": "Fear Assessment"},
             "installInfo": {"installFolder": str(tmp_path / "moved-away")}, "upload": LINUX_UPLOAD}
    daemon = _Daemon(tmp_path, caves=[stale])
    result = await _installer(daemon).install("2119837", str(tmp_path), None)
    assert result.success
    assert ("Uninstall.Perform", {"caveId": "old-cave"}) in daemon.calls


async def test_uninstall_removes_the_folder_after_butler(tmp_path: Path) -> None:
    folder = tmp_path / "fear-assessment"
    folder.mkdir()
    cave = {"id": "cave-1", "game": {"id": 2119837}, "installInfo": {"installFolder": str(folder)}}
    daemon = _Daemon(tmp_path, caves=[cave])
    result = await _installer(daemon).uninstall("2119837")
    assert result.success
    assert not folder.exists()


def test_sweep_only_removes_butler_staging(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    (downloads / "abandoned").mkdir(parents=True)
    (downloads / "abandoned" / "install-state.dat").write_text("")
    (downloads / "someones-game").mkdir()
    (downloads / "someones-game" / "save.dat").write_text("keep")
    assert sweep_staging(str(tmp_path)) == ["abandoned"]
    assert (downloads / "someones-game" / "save.dat").exists()
