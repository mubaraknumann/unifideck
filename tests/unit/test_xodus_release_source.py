"""Installing the Xodus components from a GitHub release.

A fake ``open_url`` serves a release list and the three assets from memory,
built from the same fake ``out/`` the local-folder tests use. No network.
``XDG_DATA_HOME`` points under ``tmp_path``.
"""
from __future__ import annotations

import io
import json
import tarfile
import threading
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from _xodus_backend_fakes import make_source

from unifideck.launcher.xbox_pc import backend, layout
from unifideck.stores.microsoft.xodus import release_source as rs
from unifideck.stores.microsoft.xodus import runtime as rt
from unifideck.stores.microsoft.xodus.runtime_source import SourceError, source_from_config
from unifideck.stores.microsoft.xodus.runtime_status import probe_runtime

DL = f"https://github.com/{rs.REPO}/releases/download"
BACKEND = "unifideck-xodus-backend-0.1.0-x86_64.tar.xz"


@pytest.fixture(autouse=True)
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(rt, "SPACE_MARGIN_BYTES", 0)
    monkeypatch.setattr(rt, "SPACE_FACTOR", 1)
    backend._DIGEST_CACHE.clear()
    return tmp_path


class _Response:
    def __init__(self, body: bytes, status: int = 200, cut_at: int | None = None) -> None:
        self.status = status
        self._buf = io.BytesIO(body if cut_at is None else body[:cut_at])
        self._cut = cut_at is not None

    def read(self, n: int = -1) -> bytes:
        data = self._buf.read(n)
        if not data and self._cut:
            raise urllib.error.URLError("connection reset")
        return data

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class FakeGitHub:
    """Serves ``releases`` and ``files``; records every request."""

    def __init__(self, files: dict[str, bytes], releases: list[dict[str, Any]]) -> None:
        self.files = files
        self.releases = releases
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.cut_at: int | None = None
        self.down = False

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> _Response:
        self.requests.append((url, dict(headers)))
        if self.down:
            raise urllib.error.URLError("offline")
        if url == rs.RELEASES_URL:
            return _Response(json.dumps(self.releases).encode())
        body = self.files[url.rsplit("/", 1)[1]]
        start = int(headers["Range"][6:-1]) if "Range" in headers else 0
        cut, self.cut_at = self.cut_at, None
        return _Response(body[start:], 206 if start else 200, cut)

    def asset_requests(self) -> list[tuple[str, dict[str, str]]]:
        return [r for r in self.requests if r[0] != rs.RELEASES_URL]


def _assets(tag: str, files: dict[str, bytes]) -> list[dict[str, Any]]:
    return [{"name": n, "browser_download_url": f"{DL}/{tag}/{n}", "size": len(b)} for n, b in files.items()]


def _release(tag: str, published: str, files: dict[str, bytes], **extra: Any) -> dict[str, Any]:
    return {"tag_name": tag, "published_at": published, "assets": _assets(tag, files), **extra}


def _files(tmp: Path, version: str = "0.1.10") -> dict[str, bytes]:
    out = make_source(tmp / version, version=version)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name in ("xodus-cli", "xodus-service", "LICENSE", "manifest.json"):
            tf.add(out / "backend" / name, arcname=name)
    files = {BACKEND: buf.getvalue()}
    for suffix in ("-x86_64.tar.xz", "-x86_64.sha512sum"):
        name = f"unifideck-xodus-runtime-{version}{suffix}"
        files[name] = (out / "runtime" / name).read_bytes()
    return files


def _github(tmp: Path, version: str = "0.1.10") -> FakeGitHub:
    files = _files(tmp, version)
    return FakeGitHub(files, [_release("xodus-components", "2026-10-23T10:00:00Z", files)])


def _install(gh: FakeGitHub, progress: Any = None) -> layout.ActiveComponents:
    source = rs.ReleaseSource(open_url=gh)
    return rt.install_sync(source, progress or (lambda *a: None), threading.Event())


def test_newest_complete_release_wins(tmp_path: Path) -> None:
    files = _files(tmp_path)
    partial = {k: v for k, v in files.items() if not k.endswith(".sha512sum")}
    foreign = _assets("x", files)
    for asset in foreign:
        asset["browser_download_url"] = "https://example.invalid/" + asset["name"]
    releases = [
        _release("old", "2026-10-01T00:00:00Z", files),
        _release("partial", "2026-10-25T00:00:00Z", partial),
        _release("draft", "2026-10-26T00:00:00Z", files, draft=True),
        {"tag_name": "foreign", "published_at": "2026-10-27T00:00:00Z", "assets": foreign},
        _release("Dev-20261023", "2026-10-23T00:00:00Z", files),
    ]
    offer = rs.newest_offer(releases)
    assert offer is not None and offer.tag == "Dev-20261023"
    assert (offer.runtime_version, offer.backend_version) == ("0.1.10", "0.1.0")
    assert rs.newest_offer([releases[1]]) is None


def test_offer_is_cached_and_failures_briefly(tmp_path: Path) -> None:
    gh, now = _github(tmp_path), [1000.0]
    source = rs.ReleaseSource(open_url=gh, clock=lambda: now[0])
    first = source.offer()
    now[0] += rs.OFFER_TTL_S - 1
    assert rs.ReleaseSource(open_url=gh, clock=lambda: now[0]).offer() == first
    assert len(gh.requests) == 1
    now[0] += 2
    gh.down = True
    with pytest.raises(SourceError) as err:
        source.offer()
    assert err.value.code == f"release_unreachable:{rs.WHERE}"
    with pytest.raises(SourceError):
        source.offer()
    assert len(gh.requests) == 2
    gh.down, gh.releases = False, []
    now[0] += rs.FAILURE_TTL_S
    with pytest.raises(SourceError) as err:
        source.offer()
    assert err.value.code == f"release_not_found:{rs.WHERE}"


def test_install_downloads_then_deletes_the_download(tmp_path: Path) -> None:
    gh = _github(tmp_path)
    stages: list[str] = []
    active = _install(gh, lambda stage, done, total: stages.append(stage))
    assert active.runtime_version == "0.1.10" and active.backend_version == "0.1.0"
    assert backend.check_backend(active)[1] is None
    assert "downloading" in stages and stages.index("downloading") < stages.index("extracting")
    assert not any(rs.download_root().iterdir())
    fetched = len(gh.asset_requests())
    assert _install(gh) == active
    assert len(gh.asset_requests()) == fetched


def test_a_cut_download_resumes(tmp_path: Path) -> None:
    gh = _github(tmp_path)
    gh.cut_at = 7
    with pytest.raises(rt.ComponentInstallError) as err:
        _install(gh)
    first = gh.asset_requests()[0][0].rsplit("/", 1)[1]
    assert err.value.code == f"release_download_failed:{first}"
    parts = list(rs.download_root().rglob("*.part"))
    assert [p.stat().st_size for p in parts] == [7]
    _install(gh)
    resumed = [h for u, h in gh.asset_requests() if u.endswith(first)]
    assert resumed[-1] == {"Range": "bytes=7-"}


def test_a_tampered_runtime_is_refused_and_discarded(tmp_path: Path) -> None:
    gh = _github(tmp_path)
    name = "unifideck-xodus-runtime-0.1.10-x86_64.tar.xz"
    gh.files[name] = gh.files[name][:-1] + bytes([gh.files[name][-1] ^ 1])
    with pytest.raises(rt.ComponentInstallError) as err:
        _install(gh)
    assert err.value.code == "runtime_checksum_mismatch"
    assert layout.read_active() is None
    assert not any(rs.download_root().iterdir())


def test_asset_names_that_disagree_with_the_contents_are_refused(tmp_path: Path) -> None:
    gh = _github(tmp_path)
    gh.files["unifideck-xodus-backend-0.1.2-x86_64.tar.xz"] = gh.files.pop(BACKEND)
    gh.releases = [_release("x", "2026-10-23T10:00:00Z", gh.files)]
    with pytest.raises(rt.ComponentInstallError) as err:
        _install(gh)
    assert err.value.code == "release_mismatch:unifideck-xodus-backend-0.1.2-x86_64.tar.xz"
    assert layout.read_active() is None
    assert not any(rs.download_root().iterdir())


def test_a_newer_release_updates_an_older_never(tmp_path: Path) -> None:
    old = _install(_github(tmp_path / "a", "0.1.10"))
    assert probe_runtime("", enabled=True)["state"] == "installed"
    newer = _github(tmp_path / "b", "0.1.11")
    newer.releases[0]["published_at"] = "2026-10-24T00:00:00Z"
    (layout.data_root() / "release.json").unlink()
    rs_view = probe_runtime_with(newer)
    assert rs_view["state"] == "outdated" and rs_view["source_dir"] == rs.WHERE
    assert _install(newer).runtime_version == "0.1.11"
    older = _github(tmp_path / "c", "0.1.9")
    (layout.data_root() / "release.json").unlink()
    assert _install(older).runtime_version == "0.1.11"
    assert old.runtime_version == "0.1.10"


def probe_runtime_with(gh: FakeGitHub, enabled: bool = True) -> dict[str, Any]:
    rs.ReleaseSource(open_url=gh).offer()  # fills the cache probe_runtime reads
    return probe_runtime("", enabled=enabled)


def test_probe_skips_github_while_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(_github(tmp_path))

    def boom(*a: Any) -> Any:
        raise AssertionError("network while off")

    monkeypatch.setattr(rs, "_open_url", boom)
    (layout.data_root() / "release.json").unlink()
    view = probe_runtime("github", enabled=False)
    assert view["state"] == "installed" and view["version"] == "0.1.10"


def test_config_picks_the_source() -> None:
    assert isinstance(source_from_config(""), rs.ReleaseSource)
    assert isinstance(source_from_config(" github "), rs.ReleaseSource)
    assert source_from_config("~/x").where == str(Path("~/x").expanduser())
