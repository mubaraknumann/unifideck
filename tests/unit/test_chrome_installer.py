"""Tests for the plugin's own browser download (auth/edge_browser/chrome_installer).

Every browser window the plugin opens depends on it since it replaced the Edge
flatpak. They pin what a wrong download must never become: a browser that
looks installed. A wrong checksum, a lost executable bit, a path escaping the
folder and a build that does not run are all refused, and two callers never
download at once.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import stat
import tarfile
from pathlib import Path
from typing import Any

import pytest

from unifideck.auth.edge_browser import chrome_installer as ci

FAKE_CHROME = "#!/bin/sh\necho 'Chromium {version} '\n"


def _archive(version: str, *, mode: int = 0o755, extra: str | None = None,
             link: str | None = None) -> bytes:
    """A ``.tar.xz`` laid out like the real release: ``<archive dir>/chrome``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        folder = tarfile.TarInfo(ci._ARCHIVE_DIR)
        folder.type, folder.mode = tarfile.DIRTYPE, 0o755
        tar.addfile(folder)
        body = FAKE_CHROME.format(version=version).encode()
        info = tarfile.TarInfo(f"{ci._ARCHIVE_DIR}/chrome")
        info.size, info.mode = len(body), mode
        tar.addfile(info, io.BytesIO(body))
        if extra:
            other = tarfile.TarInfo(extra)
            other.size = 1
            tar.addfile(other, io.BytesIO(b"x"))
        if link:
            sym = tarfile.TarInfo(f"{ci._ARCHIVE_DIR}/{link}")
            sym.type, sym.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(sym)
    return buf.getvalue()


@pytest.fixture()
def offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Serve ``payload`` as the download, into ``tmp_path``; return the call log."""
    monkeypatch.setattr(ci, "browser_root", lambda: tmp_path / "browser")
    downloads: list[int] = []

    def serve(payload: bytes, *, pin: bool = True) -> list[int]:
        if pin:
            monkeypatch.setattr(ci, "CHROME_SHA256", hashlib.sha256(payload).hexdigest())

        def download(dest: Path) -> str:
            downloads.append(1)
            dest.write_bytes(payload)
            return hashlib.sha256(payload).hexdigest()

        monkeypatch.setattr(ci, "_download", download)
        return downloads

    return serve


def test_a_good_download_is_installed_and_runnable(offline: Any) -> None:
    offline(_archive(ci.CHROME_VERSION))
    assert ci.install_blocking() == {"success": True, "message": "microsoft.browserInstalled"}
    assert ci.is_ready()
    assert ci.ChromeInstaller().find_cmd() == [str(ci.chrome_binary())]


def test_the_executable_bit_survives_unpacking(offline: Any) -> None:
    """A chrome without it is "present but broken" (CLAUDE.md rule 7)."""
    offline(_archive(ci.CHROME_VERSION))
    ci.install_blocking()
    assert ci.chrome_binary().stat().st_mode & stat.S_IXUSR


def test_a_wrong_checksum_is_never_installed(offline: Any) -> None:
    offline(_archive(ci.CHROME_VERSION), pin=False)
    assert ci.install_blocking()["success"] is False
    assert not ci.is_ready() and not ci.install_dir().exists()


def test_a_build_that_does_not_answer_its_version_is_refused(offline: Any) -> None:
    offline(_archive("1.2.3"))
    assert ci.install_blocking()["success"] is False
    assert not ci.is_ready()


def test_a_path_escaping_the_folder_is_refused(offline: Any, tmp_path: Path) -> None:
    offline(_archive(ci.CHROME_VERSION, extra="../../escaped"))
    assert ci.install_blocking()["success"] is False
    assert not (tmp_path / "escaped").exists()


def test_a_ready_browser_is_not_downloaded_again(offline: Any) -> None:
    downloads = offline(_archive(ci.CHROME_VERSION))
    ci.install_blocking()
    assert ci.install_blocking()["message"] == "microsoft.browserAlreadyInstalled"
    assert len(downloads) == 1


def test_older_builds_are_removed_after_an_upgrade(offline: Any) -> None:
    # ``chrome-*`` is the Chrome for Testing build 0.7.7 shipped first.
    older = [ci.browser_root() / "chromium-1.0.0", ci.browser_root() / "chrome-156.0.8078.80"]
    for folder in older:
        folder.mkdir(parents=True)
    offline(_archive(ci.CHROME_VERSION))
    ci.install_blocking()
    assert not any(folder.exists() for folder in older)


def test_a_link_in_the_archive_is_refused(offline: Any) -> None:
    offline(_archive(ci.CHROME_VERSION, link="passwd"))
    assert ci.install_blocking()["success"] is False
    assert not ci.is_ready()


def test_no_download_leaves_no_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci, "browser_root", lambda: tmp_path / "browser")

    def offline_download(_dest: Path) -> str:
        raise OSError("network down")

    monkeypatch.setattr(ci, "_download", offline_download)
    assert ci.install_blocking() == {"success": False, "error": "microsoft.chromiumInstallTimeout"}
    assert ci.ChromeInstaller().find_cmd() is None


async def test_two_callers_never_download_at_once(offline: Any) -> None:
    """The startup download and the install modal share the staging paths."""
    downloads = offline(_archive(ci.CHROME_VERSION))
    installer = ci.ChromeInstaller()
    first, second = await asyncio.gather(installer.install(), installer.install())
    assert len(downloads) == 1
    assert {first.get("message"), second.get("message")} == {
        "microsoft.browserInstalled", "microsoft.browserAlreadyInstalled"}


async def test_startup_fetches_only_a_missing_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from unifideck.auth.edge_browser import EdgeBrowser

    calls: list[str] = []

    async def install(_self: Any) -> dict[str, Any]:
        calls.append("install")
        return {"success": True}

    monkeypatch.setattr(EdgeBrowser, "install", install)
    browser = EdgeBrowser.__new__(EdgeBrowser)
    browser._install_task = None

    monkeypatch.setattr(EdgeBrowser, "is_installed", property(lambda _self: True))
    await browser.start()
    assert browser._install_task is None

    monkeypatch.setattr(EdgeBrowser, "is_installed", property(lambda _self: False))
    await browser.start()
    assert browser._install_task is not None
    await browser._install_task
    assert calls == ["install"]
