"""stores/microsoft/xodus/release_source.py — the components from GitHub.

The default source. The plugin's own GitHub releases carry three assets,
uploaded by hand to any release (a dev push deletes older ``Dev-*``
releases, so a release of their own is the safe home)::

    unifideck-xodus-backend-<ver>-x86_64.tar.xz   xodus-cli  xodus-service
                                                  LICENSE  manifest.json
    unifideck-xodus-runtime-<ver>-x86_64.tar.xz
    unifideck-xodus-runtime-<ver>-x86_64.sha512sum

The newest published release that has all three wins; the tag is never
pinned, so a new runtime ships without a plugin build.

:meth:`ReleaseSource.offer` reads the release list. It is cached for an
hour (in memory and in ``release.json``), and a failure for five minutes,
so a status refresh rarely touches the network. :meth:`ReleaseSource.fetch`
downloads into ``download/<tag>/`` in the same ``backend/`` + ``runtime/``
layout as the build repo's ``out/``, and hands that folder to
:class:`LocalDirSource`. Every checksum check after that is the local
install's. An interrupted download resumes from its ``.part`` file.

All of it is blocking: call it from a worker thread.
"""
from __future__ import annotations

import json
import re
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from unifideck.core.net.ssl_helpers import ssl_ctx_strict
from unifideck.launcher.xbox_pc.layout import (
    BACKEND_MANIFEST_NAME,
    CLI_NAME,
    SERVICE_NAME,
    data_root,
    ensure_private_dir,
)

from .runtime_source import (
    CHECKSUM_SUFFIX,
    RUNTIME_PREFIX,
    RUNTIME_SUFFIX,
    LocalDirSource,
    SourceError,
    SourceListing,
)

REPO = "mubaraknumann/unifideck"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
#: What the QAM names as the place it looked.
WHERE = f"github.com/{REPO}"
BACKEND_PREFIX = "unifideck-xodus-backend-"
BACKEND_SUFFIX = "-x86_64.tar.xz"
OFFER_TTL_S = 3600.0
FAILURE_TTL_S = 300.0
API_TIMEOUT_S = 15.0
DOWNLOAD_TIMEOUT_S = 60.0
_CHUNK = 1 << 20
_REPORT_EVERY = 4 << 20
_VERSION = r"([0-9][0-9A-Za-z.+_-]{0,63})"
_BACKEND_RE = re.compile(re.escape(BACKEND_PREFIX) + _VERSION + re.escape(BACKEND_SUFFIX) + r"\Z")
_RUNTIME_RE = re.compile(re.escape(RUNTIME_PREFIX) + _VERSION + re.escape(RUNTIME_SUFFIX) + r"\Z")
_TAG_RE = re.compile(r"[^A-Za-z0-9._-]")
#: The only names the backend tarball may hold.
_BACKEND_FILES = frozenset({CLI_NAME, SERVICE_NAME, "LICENSE", BACKEND_MANIFEST_NAME})

#: ``progress(stage, done_bytes, total_bytes)``, as in ``runtime.py``.
ProgressFn = Callable[[str, int, "int | None"], None]
#: ``open_url(url, headers, timeout)`` returns a response like urllib's.
Opener = Callable[[str, dict[str, str], float], Any]


@dataclass(frozen=True)
class ReleaseAsset:
    """One downloadable file of a release."""

    name: str
    url: str
    size: int


@dataclass(frozen=True)
class ReleaseOffer:
    """The components the newest complete release offers."""

    tag: str
    published_at: str
    backend_version: str
    runtime_version: str
    backend: ReleaseAsset
    runtime: ReleaseAsset
    checksum: ReleaseAsset

    def assets(self) -> tuple[ReleaseAsset, ...]:
        """The files to download, smallest first."""
        return (self.checksum, self.backend, self.runtime)

    def total_bytes(self) -> int:
        """The download size of all three."""
        return sum(a.size for a in self.assets())

    @classmethod
    def from_json(cls, data: Any) -> ReleaseOffer | None:
        """An offer saved by :func:`dataclasses.asdict`, or None when unreadable."""
        try:
            assets = {k: ReleaseAsset(**data[k]) for k in ("backend", "runtime", "checksum")}
            return cls(
                tag=str(data["tag"]), published_at=str(data["published_at"]),
                backend_version=str(data["backend_version"]),
                runtime_version=str(data["runtime_version"]), **assets,
            )
        except (KeyError, TypeError):
            return None


def _asset(raw: Any) -> ReleaseAsset | None:
    if not isinstance(raw, dict):
        return None
    name, url, size = raw.get("name"), raw.get("browser_download_url"), raw.get("size")
    if not isinstance(name, str) or not isinstance(url, str) or not isinstance(size, int):
        return None
    if not url.startswith(f"https://github.com/{REPO}/releases/download/") or size <= 0:
        return None
    return ReleaseAsset(name, url, size)


def _match(assets: dict[str, ReleaseAsset], pattern: re.Pattern[str]) -> tuple[ReleaseAsset, str] | None:
    found = [(a, m.group(1)) for a in assets.values() if (m := pattern.match(a.name))]
    return found[0] if len(found) == 1 else None


def offer_from_release(release: Any) -> ReleaseOffer | None:
    """The offer of one release from the API, or None when it lacks a part."""
    if not isinstance(release, dict) or release.get("draft"):
        return None
    tag, published = release.get("tag_name"), release.get("published_at")
    raw_assets = release.get("assets")
    if not isinstance(tag, str) or not isinstance(published, str) or not isinstance(raw_assets, list):
        return None
    assets = {a.name: a for a in map(_asset, raw_assets) if a is not None}
    backend, runtime = _match(assets, _BACKEND_RE), _match(assets, _RUNTIME_RE)
    if backend is None or runtime is None:
        return None
    checksum = assets.get(f"{RUNTIME_PREFIX}{runtime[1]}{CHECKSUM_SUFFIX}")
    if checksum is None:
        return None
    return ReleaseOffer(tag, published, backend[1], runtime[1], backend[0], runtime[0], checksum)


def newest_offer(releases: Any) -> ReleaseOffer | None:
    """The newest published release that has all three assets."""
    offers = [o for o in map(offer_from_release, releases if isinstance(releases, list) else []) if o]
    return max(offers, key=lambda o: o.published_at, default=None)


def _open_url(url: str, headers: dict[str, str], timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "Unifideck", **headers})
    return urllib.request.urlopen(request, timeout=timeout, context=ssl_ctx_strict())


def download_root() -> Path:
    """Where downloads wait to be installed."""
    return data_root() / "download"


def _offer_cache_path() -> Path:
    return data_root() / "release.json"


class ReleaseSource:
    """The newest complete GitHub release of the plugin."""

    where = WHERE

    def __init__(self, open_url: Opener = _open_url, clock: Callable[[], float] = time.time) -> None:
        """``open_url`` and ``clock`` are replaced in tests."""
        self._open = open_url
        self._clock = clock
        self._lock = threading.Lock()

    # ── Discover ───────────────────────────────────────────────────────

    def offer(self) -> ReleaseOffer:
        """The newest complete release, cached. Raises ``SourceError``."""
        with self._lock:
            return self._offer_locked()

    def _offer_locked(self) -> ReleaseOffer:
        now = self._clock()
        cached = _read_cache()
        if cached is not None and now - cached[0] < OFFER_TTL_S and cached[1] is not None:
            return cached[1]
        if cached is not None and now - cached[0] < FAILURE_TTL_S and cached[2]:
            raise SourceError(cached[2])
        try:
            offer = self._discover()
        except SourceError as e:
            _write_cache(now, None, e.code)
            raise
        _write_cache(now, offer, "")
        return offer

    def _discover(self) -> ReleaseOffer:
        headers = {"Accept": "application/vnd.github+json"}
        try:
            with self._open(RELEASES_URL, headers, API_TIMEOUT_S) as response:
                releases = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError):
            raise SourceError(f"release_unreachable:{WHERE}") from None
        offer = newest_offer(releases)
        if offer is None:
            raise SourceError(f"release_not_found:{WHERE}")
        return offer

    # ── Download ───────────────────────────────────────────────────────

    def folder(self, offer: ReleaseOffer) -> Path:
        """``download/<tag>/``, laid out like the build repo's ``out/``."""
        return download_root() / (_TAG_RE.sub("_", offer.tag) or "release")

    def fetch(self, offer: ReleaseOffer, progress: ProgressFn, abort: threading.Event) -> SourceListing:
        """Download ``offer`` (resuming) and describe it. Raises ``SourceError``."""
        out = _prepare_folder(self.folder(offer))
        meter = _Meter(offer.total_bytes(), progress)
        tarball = out / offer.backend.name
        for asset in offer.assets():
            target = tarball if asset is offer.backend else out / "runtime" / asset.name
            self._download(asset, target, meter, abort)
        _unpack_backend(tarball, out / "backend")
        return LocalDirSource(out).describe()

    def _download(self, asset: ReleaseAsset, target: Path, meter: _Meter, abort: threading.Event) -> None:
        if target.is_file() and target.stat().st_size == asset.size:
            meter.add(asset.size)
            return
        part = target.with_name(target.name + ".part")
        have = part.stat().st_size if part.is_file() else 0
        if have >= asset.size:
            part.unlink()
            have = 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with self._open(asset.url, headers, DOWNLOAD_TIMEOUT_S) as response:
                if getattr(response, "status", 200) != 206:
                    have = 0  # The server ignored the range: start over.
                meter.add(have)
                _write_body(response, part, bool(have), meter, abort)
        except (urllib.error.URLError, OSError):
            raise SourceError(f"release_download_failed:{asset.name}") from None
        if part.stat().st_size != asset.size:
            part.unlink()
            raise SourceError(f"release_download_failed:{asset.name}")
        part.replace(target)

    def discard(self, offer: ReleaseOffer) -> None:
        """Delete the download of ``offer`` (installed, or found bad)."""
        shutil.rmtree(self.folder(offer), ignore_errors=True)


class _Meter:
    """Adds up the bytes of all three downloads into one ``downloading`` stage."""

    def __init__(self, total: int, progress: ProgressFn) -> None:
        self._total = total
        self._progress = progress
        self._done = 0
        self._reported = -_REPORT_EVERY

    def add(self, n: int) -> None:
        self._done += n
        if self._done - self._reported >= _REPORT_EVERY or self._done >= self._total:
            self._reported = self._done
            self._progress("downloading", min(self._done, self._total), self._total)


def _write_body(response: Any, part: Path, append: bool, meter: _Meter, abort: threading.Event) -> None:
    with part.open("ab" if append else "wb") as fh:
        while chunk := response.read(_CHUNK):
            if abort.is_set():
                raise SourceError("cancelled")
            fh.write(chunk)
            meter.add(len(chunk))


def _prepare_folder(out: Path) -> Path:
    """Make ``out``'s ``backend/`` and ``runtime/``, dropping other downloads."""
    ensure_private_dir(data_root())
    ensure_private_dir(download_root())
    _drop_other_downloads(keep=out)
    (out / "backend").mkdir(parents=True, exist_ok=True)
    (out / "runtime").mkdir(parents=True, exist_ok=True)
    return out


def _unpack_backend(tarball: Path, dest: Path) -> None:
    """Write the tarball's flat files into ``dest``. Anything else is refused."""
    try:
        with tarfile.open(tarball, mode="r:xz") as tf:
            for member in tf:
                if not member.isfile() or member.name not in _BACKEND_FILES:
                    raise SourceError(f"source_incomplete:{tarball.name}")
                reader = tf.extractfile(member)
                if reader is None:
                    raise SourceError(f"source_incomplete:{tarball.name}")
                with reader, (dest / member.name).open("wb") as fh:
                    shutil.copyfileobj(reader, fh)
    except (tarfile.TarError, OSError, EOFError):
        tarball.unlink(missing_ok=True)
        raise SourceError(f"source_incomplete:{tarball.name}") from None


def _drop_other_downloads(keep: Path) -> None:
    """A newer release replaces an unfinished download of an older one."""
    for path in download_root().iterdir():
        if path != keep and path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)


def _read_cache() -> tuple[float, ReleaseOffer | None, str] | None:
    try:
        data = json.loads(_offer_cache_path().read_text(encoding="utf-8"))
        at, error = float(data["at"]), str(data.get("error") or "")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    offer = ReleaseOffer.from_json(data.get("offer")) if data.get("offer") else None
    return at, offer, error


def _write_cache(at: float, offer: ReleaseOffer | None, error: str) -> None:
    data = {"at": at, "offer": asdict(offer) if offer else None, "error": error}
    try:
        ensure_private_dir(data_root())
        tmp = _offer_cache_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(_offer_cache_path())
    except OSError:
        pass  # The cache only saves a request; the next call asks again.
