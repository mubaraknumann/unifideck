"""launcher/proton/infrastructure/ge_installer.py — Latest GE-Proton fetch/install.

Downloads and installs the newest GE-Proton release from GitHub
(``GloriousEggroll/proton-ge-custom``) into Steam's
``compatibilitytools.d`` so games default to the *latest* GE-Proton
released online, not merely the newest copy already on disk.

Used from two processes:

* the plugin (Decky's Python) on startup — background, non-blocking,
  via ``ProtonService.start`` → ``asyncio.to_thread``;
* the launcher (system Python) as a launch-time safety net inside
  ``selector.select_proton_version``.

Every network/disk failure is swallowed and surfaced as ``None`` so
the caller can fall back to Proton Experimental. Only the stdlib is
used (``urllib`` / ``tarfile`` / ``json``) — the slim launcher has no
third-party HTTP client.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import ssl
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unifideck.launcher.proton.infrastructure.ge_install_lock import (
    install_lock,
)

logger = logging.getLogger(__name__)

GE_REPO = "GloriousEggroll/proton-ge-custom"
_LATEST_API = f"https://api.github.com/repos/{GE_REPO}/releases/latest"
_USER_AGENT = "unifideck-proton-ge"

# SteamOS's cert store is too old to verify GitHub's chain under strict TLS,
# so GE-Proton lookups/downloads fail with CERTIFICATE_VERIFY_FAILED (the
# plugin disables verification everywhere for this reason — see
# ``core.net.ssl_helpers``). Kept local + stdlib-only so this module stays
# importable in the minimal launcher bootstrap (no ``unifideck.*`` deps).
_permissive_ssl_ctx: ssl.SSLContext | None = None


def _ssl_ctx() -> ssl.SSLContext:
    """Return the shared permissive TLS context (hostname + chain checks off)."""
    global _permissive_ssl_ctx
    if _permissive_ssl_ctx is None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _permissive_ssl_ctx = ctx
    return _permissive_ssl_ctx

# Install target — the primary root the selector scans first.
COMPAT_TOOLS_DIR = Path("~/.steam/root/compatibilitytools.d").expanduser()
# Roots scanned to decide whether a tag is already installed. Mirrors
# ``selector.STEAM_COMPAT_ROOTS`` (kept local to avoid a circular
# import — selector imports this module, not the other way round).
_SCAN_ROOTS: tuple[str, ...] = (
    "~/.steam/root/compatibilitytools.d",
    "~/.steam/steam/compatibilitytools.d",
    "~/.local/share/Steam/compatibilitytools.d",
)
# Records the tag the background installer last validated, so the
# launcher can resolve the default without a network round-trip.
_MARKER = Path("~/.local/share/unifideck/proton_ge_latest.json").expanduser()

ProgressCb = Callable[[int, int], None]


def _fetch_latest_release(timeout: float) -> dict[str, Any] | None:
    """GET the GitHub ``/releases/latest`` JSON, or ``None`` on failure."""
    req = urllib.request.Request(
        _LATEST_API,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            return json.loads(resp.read().decode())  # type: ignore[no-any-return]
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.warning("[ge_installer] latest-release lookup failed: %s", e)
        return None


def get_latest_ge_tag(timeout: float = 8.0) -> str | None:
    """Return the newest GE-Proton release tag, or ``None`` on failure."""
    release = _fetch_latest_release(timeout)
    if not release:
        return None
    tag = release.get("tag_name")
    return tag or None


def _installed_proton_script(tag: str) -> Path | None:
    """The on-disk ``proton`` script for ``tag`` if present in any root."""
    for root in _SCAN_ROOTS:
        candidate = Path(root).expanduser() / tag / "proton"
        if candidate.is_file():
            return candidate
    return None


def installed_ge_proton_path(tag: str) -> Path | None:
    """Return ``tag``'s ``proton`` script only if it is validly installed.

    A directory can survive a partial/aborted extract whose ``proton``
    is left non-executable (observed with a real GE-Proton10-34 on
    disk). Such a copy would be picked as "newest present" but die
    with "Permission denied" on exec — so an install is only valid
    when the ``proton`` script is BOTH present and executable.
    """
    script = _installed_proton_script(tag)
    if script and os.access(script, os.X_OK):
        return script
    return None


def is_valid_ge_install(tag: str) -> bool:
    """True iff ``tag`` is installed with an executable ``proton`` script."""
    return installed_ge_proton_path(tag) is not None


def _toolmanifest_ok(root: Path) -> bool:
    """True iff ``root/toolmanifest.vdf`` exists and can yield a manifest.

    umu reads this file for every launch and trusts ``is_file()`` alone, so
    the two states worth rejecting are the two it does not catch: a missing
    file, and a present-but-empty one (the shape a truncated download or an
    interrupted extract leaves behind). A cheap substring check for the
    ``manifest`` key is enough to tell "real VDF" from "empty or garbage"
    without taking a VDF-parser dependency in the launcher layer.
    """
    manifest = root / "toolmanifest.vdf"
    try:
        if not manifest.is_file() or manifest.stat().st_size == 0:
            logger.warning(
                "[ge_installer] %s has a missing/empty toolmanifest.vdf — "
                "umu would raise KeyError('manifest') on it", root.name,
            )
            return False
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning(
            "[ge_installer] toolmanifest.vdf unreadable in %s: %s", root.name, e,
        )
        return False
    if "manifest" not in text.lower():
        logger.warning(
            "[ge_installer] %s toolmanifest.vdf has no 'manifest' block — "
            "treating the build as incomplete", root.name,
        )
        return False
    return True


def is_proton_install_complete(proton_script: Path) -> bool:
    """True iff ``proton_script``'s install looks complete and runnable.

    Guards against a partially-installed / corrupt Proton being handed
    to umu, where every ``umu-run`` operation (createprefix, the
    winetricks/regedit compat steps) hangs its wineserver forever — a
    broken auto-updated Proton-Experimental build did exactly this and
    wedged the serial install queue.

    Checks the load-bearing pieces of a Proton tool directory:

    * the ``proton`` launcher script is present **and executable**
      (mirrors :func:`installed_ge_proton_path`'s "present AND
      ``os.access(X_OK)``" guard — a survived-partial-extract copy is
      left non-executable);
    * a ``files/`` subdir (the Wine/runtime payload) exists and is
      non-empty, with the Wine loader (``files/bin/wine``) present —
      the payload a hung wineserver needs and the piece a truncated
      extract most often lacks;
    * a readable, non-empty ``version`` marker;
    * a readable, non-empty ``toolmanifest.vdf``. umu parses this file
      before it launches anything (``CompatLayer.__init__`` does
      ``vdf.load(f)["manifest"]``) and guards only ``is_file()`` — so a
      TRUNCATED, zero-byte manifest sails through umu's own check, then
      ``vdf.load`` returns ``{}`` and the ``["manifest"]`` lookup raises an
      unhandled ``KeyError: 'manifest'``. That aborts the launch with a
      bare traceback and no actionable message. It is a real field failure
      (umu-launcher#706, where upstream traced it to a 0-byte manifest in
      the reporter's own Proton copy), and we download GE-Proton
      ourselves, so a partial download lands squarely in it. Rejecting the
      build here makes it fail *selection* instead, which drops through
      the existing ladder to Proton Experimental.

    NB: a zero-byte ``dist.lock`` is *not* a corruption signal —
    every official Steam Proton tool ships one as its normal per-tool
    lock, so it is deliberately not checked here.

    Best-effort and conservative: any unreadable/unexpected state is
    treated as *incomplete* so the caller degrades to a known-good
    Proton rather than risk a hang.
    """
    try:
        if not (proton_script.is_file() and os.access(proton_script, os.X_OK)):
            return False
        root = proton_script.parent
        files_dir = root / "files"
        if not files_dir.is_dir() or not any(files_dir.iterdir()):
            return False
        if not (files_dir / "bin" / "wine").is_file():
            return False
        version = root / "version"
        if not version.is_file() or version.stat().st_size == 0:
            return False
        if not _toolmanifest_ok(root):
            return False
    except OSError as e:
        logger.warning(
            "[ge_installer] completeness check failed for %s: %s",
            proton_script, e,
        )
        return False
    return True


def read_cached_latest_tag() -> str | None:
    """Return the tag the background installer last validated, if any."""
    if not _MARKER.is_file():
        return None
    try:
        data = json.loads(_MARKER.read_text())
    except (OSError, ValueError):
        return None
    tag = data.get("tag")
    return tag or None


def _write_marker(tag: str) -> None:
    """Record ``tag`` as the validated latest GE-Proton (best effort)."""
    try:
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.write_text(json.dumps({"tag": tag, "installed_at": time.time()}))
    except OSError as e:
        logger.warning("[ge_installer] could not write marker: %s", e)


def _select_tarball(assets: list[dict[str, Any]], tag: str | None = None) -> str | None:
    """Pick the GE-Proton x86_64 ``.tar.gz`` asset URL.

    GE's asset naming changed at GE-Proton11-4: the x86_64 build went from
    a bare ``<tag>.tar.gz`` to ``<tag>-x86_64.tar.gz``, alongside the
    aarch64 build that had already started shipping. Both spellings are
    matched by exact name first, then by the ``-x86_64`` suffix.

    The deny-list scan is kept last as a safety net, but it is only
    correct while every non-x86 asset carries one of the arch markers it
    knows about — a future ``riscv64``/``ppc64le`` build would slip
    through it — so the positive matches deliberately run first.
    """
    urls = {a.get("name", ""): a.get("browser_download_url") for a in assets}

    if tag:
        for expected in (f"{tag}-x86_64.tar.gz", f"{tag}.tar.gz"):
            if urls.get(expected):
                return urls[expected]

    for name, url in urls.items():
        if name.endswith("-x86_64.tar.gz"):
            return url

    for name, url in urls.items():
        if name.endswith(".tar.gz") and not any(k in name for k in ("sha512", "aarch64", "arm64")):
            return url
    return None



def _download(url: str, dest: Path, progress_cb: ProgressCb | None) -> None:
    """Stream ``url`` to ``dest``, reporting bytes via ``progress_cb``.

    Raises ``OSError`` if the server declared a ``Content-Length`` and the
    body came up short. A dropped connection mid-stream ends the read loop
    exactly like a clean EOF, so without this comparison a truncated
    tarball was written and returned as a SUCCESS. That is how a corrupt
    GE-Proton reaches the compat tools dir, and from there umu dies on the
    half-written ``toolmanifest.vdf`` (see :func:`_toolmanifest_ok`).
    ``OSError`` is deliberate: it is already what
    :func:`_download_with_retry` catches, so a short read now costs a
    retry with backoff instead of a broken install.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx()) as resp, dest.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress_cb:
                progress_cb(done, total)
    if total and done != total:
        msg = (
            f"truncated download: got {done} of {total} bytes "
            f"({done * 100 // total}%) from {url}"
        )
        raise OSError(msg)


def _download_with_retry(
    url: str,
    dest: Path,
    progress_cb: ProgressCb | None,
    attempts: int = 3,
) -> bool:
    """Download with exponential backoff (5/10/20s). True on success."""
    for attempt in range(1, attempts + 1):
        try:
            _download(url, dest, progress_cb)
        except (urllib.error.URLError, OSError) as e:
            logger.warning(
                "[ge_installer] download attempt %d/%d failed: %s",
                attempt, attempts, e,
            )
            dest.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(5 * (2 ** (attempt - 1)))
        else:
            return True
    return False


def _extract(tarball: Path, dest: Path) -> bool:
    """Extract ``tarball`` into ``dest``. True on success."""
    try:
        with tarfile.open(tarball, "r:gz") as tar:
            try:
                # ``filter="data"`` (3.12, backported to 3.11.4) blocks
                # path-traversal/symlink escapes; the older fallback is
                # acceptable for a trusted GitHub release tarball.
                tar.extractall(dest, filter="data")
            except TypeError:
                tar.extractall(dest)  # noqa: S202 — trusted GitHub release tarball
    except (tarfile.TarError, OSError) as e:
        logger.warning("[ge_installer] extract failed: %s", e)
        return False
    return True


def _make_executable(path: Path) -> None:
    """Add the +x bit (owner/group/other) to ``path``."""
    st = path.stat()
    path.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _find_extracted_root(staging: Path, tag: str) -> Path | None:
    """Locate the extracted Proton tree inside ``staging``.

    A GE-Proton archive expands to a single top-level directory, but its
    name is NOT reliably the tag. From GE-Proton11-4 the x86_64 build
    unpacks to ``<tag>-x86_64/`` (matching the renamed asset) instead of
    ``<tag>/``, so keying off the tag alone made every install fail its
    "missing proton script" check — silently re-downloading ~500 MB on
    every plugin start and pinning users to the newest GE already on
    disk.

    The tree is therefore identified by the thing that actually defines
    it: a top-level dir holding a ``proton`` script. If a future archive
    ever ships more than one, an exact-or-``<tag>-``-prefixed name wins
    so the choice never rests on sort order.
    """
    try:
        candidates = sorted(
            d for d in staging.iterdir() if d.is_dir() and (d / "proton").is_file()
        )
    except OSError as e:
        logger.warning("[ge_installer] could not scan the staging dir: %s", e)
        return None
    if not candidates:
        return None
    for candidate in candidates:
        if candidate.name == tag or candidate.name.startswith(f"{tag}-"):
            return candidate
    return candidates[0]


def _promote_extracted(staging: Path, tag: str) -> Path | None:
    """Validate the extracted tree and move it into place as ``<tag>/``.

    The move into ``COMPAT_TOOLS_DIR`` only happens after the ``proton``
    script is confirmed present and made executable, returning the final
    executable ``proton`` path (or ``None`` if validation fails).

    The destination is always the bare ``<tag>`` regardless of what the
    archive called its top-level dir: the marker file,
    :func:`installed_ge_proton_path` and the selector all key off the tag,
    so publishing an arch-suffixed name would install fine yet still miss
    the "already installed?" check and re-download on the next start.

    ``toolmanifest.vdf`` is validated here too, while the tree is still in
    staging and gets cleaned up for free — catching a bad build before it
    is published rather than at launch. :func:`is_proton_install_complete`
    repeats the check because a tool dir can also rot after install (or
    arrive from somewhere other than this installer).
    """
    extracted = _find_extracted_root(staging, tag)
    if extracted is None:
        logger.warning(
            "[ge_installer] extracted tree missing proton script (%s)", tag,
        )
        return None
    proton = extracted / "proton"
    if not _toolmanifest_ok(extracted):
        logger.warning(
            "[ge_installer] discarding %s: extracted tree has no usable "
            "toolmanifest.vdf (truncated download?)", tag,
        )
        return None
    _make_executable(proton)

    dest = COMPAT_TOOLS_DIR / tag
    # Rename-aside, never rmtree-then-move. ``dest`` may be the Proton a game
    # or a live ``umu-run`` is executing out of right now: deleting it first
    # leaves a window with no tag directory at all, and pulls the files out
    # from under that process. Two renames on one filesystem (staging is
    # created inside COMPAT_TOOLS_DIR) leave the path always resolvable, and a
    # process holding the old tree's inodes keeps working until it exits.
    aside = COMPAT_TOOLS_DIR / f".{tag}.old-{os.getpid()}"
    try:
        if dest.exists():
            dest.rename(aside)
        extracted.rename(dest)
    except OSError as e:
        logger.warning("[ge_installer] could not publish %s: %s", tag, e)
        with contextlib.suppress(OSError):
            if aside.exists() and not dest.exists():
                aside.rename(dest)
        return None
    finally:
        shutil.rmtree(aside, ignore_errors=True)
    final = dest / "proton"
    if not os.access(final, os.X_OK):
        return None
    logger.info("[ge_installer] installed GE-Proton %s -> %s", tag, dest)
    return final


def _download_and_install(
    tag: str,
    url: str,
    progress_cb: ProgressCb | None,
) -> Path | None:
    """Download + extract ``tag`` into ``COMPAT_TOOLS_DIR``; return ``proton``.

    Stages into a temp dir on the SAME filesystem and moves the result
    into place only after the ``proton`` script is confirmed present and
    made executable — so a half-finished download never leaves a broken
    ``<tag>/`` dir that later passes a naive presence check.
    """
    COMPAT_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{tag}.dl-", dir=COMPAT_TOOLS_DIR))
    try:
        tarball = staging / f"{tag}.tar.gz"
        if not _download_with_retry(url, tarball, progress_cb):
            return None
        if not _extract(tarball, staging):
            return None
        tarball.unlink(missing_ok=True)
        return _promote_extracted(staging, tag)
    except OSError as e:
        logger.warning("[ge_installer] install of %s failed: %s", tag, e)
        return None
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def ensure_latest_ge(
    progress_cb: ProgressCb | None = None,
    timeout: float = 8.0,
) -> tuple[Path, str] | None:
    """Ensure the newest GE-Proton is installed; return ``(proton, tag)``.

    Returns ``None`` (the caller then falls back to Proton Experimental)
    when the release can't be fetched (offline / GitHub down) or the
    download/extract fails. When the latest is already validly installed
    it just refreshes the marker and returns it without downloading.

    The install itself is serialised across processes by :func:`ge_install_lock.install_lock`
    and re-checked under it with the STRONG :func:`is_proton_install_complete`
    rather than the presence-only ``installed_ge_proton_path``. The weak check
    is false during a publish, so without the re-check the loser of a race
    would download and republish over the directory the winner just installed
    — and over whatever is running out of it.
    """
    release = _fetch_latest_release(timeout)
    if not release:
        return None
    tag = release.get("tag_name")
    if not tag:
        return None

    existing = installed_ge_proton_path(tag)
    if existing:
        _write_marker(tag)
        logger.info("[ge_installer] latest GE-Proton already installed: %s", tag)
        return existing, tag

    url = _select_tarball(release.get("assets", []), tag)
    if not url:
        logger.warning("[ge_installer] no .tar.gz asset found for %s", tag)
        return None

    with install_lock():
        settled = installed_ge_proton_path(tag)
        if settled and is_proton_install_complete(settled):
            _write_marker(tag)
            logger.info(
                "[ge_installer] GE-Proton %s installed while we waited", tag,
            )
            return settled, tag
        logger.info("[ge_installer] downloading GE-Proton %s", tag)
        script = _download_and_install(tag, url, progress_cb)
    if not script:
        return None
    _write_marker(tag)
    return script, tag
