"""launcher/proton/infrastructure/ge_download.py — fetch, verify, extract a GE tarball.

Split out of ``ge_installer`` (at its volumetry cap) when the download
gained a checksum step. This module moves bytes: it streams a release
tarball, checks it against the ``.sha512sum`` GE publishes beside every
build, and unpacks it into a staging directory. Deciding *what* to install
and publishing the result stay in ``ge_installer``.

Stdlib only, for the same reason as ``ge_installer``: the launcher runs
under the system ``python3`` with no third-party HTTP client.
"""
from __future__ import annotations

import hashlib
import logging
import ssl
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

USER_AGENT = "unifideck-proton-ge"

ProgressCb = Callable[[int, int], None]

# SteamOS's cert store is too old to verify GitHub's chain under strict TLS,
# so GE-Proton lookups/downloads fail with CERTIFICATE_VERIFY_FAILED (the
# plugin disables verification everywhere for this reason — see
# ``core.net.ssl_helpers``). Kept local + stdlib-only so this module stays
# importable in the minimal launcher bootstrap (no ``unifideck.*`` deps).
# The published SHA-512 is what vouches for the tarball's content.
_permissive_ssl_ctx: ssl.SSLContext | None = None


def ssl_ctx() -> ssl.SSLContext:
    """Return the shared permissive TLS context (hostname + chain checks off)."""
    global _permissive_ssl_ctx
    if _permissive_ssl_ctx is None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _permissive_ssl_ctx = ctx
    return _permissive_ssl_ctx


def download(url: str, dest: Path, progress_cb: ProgressCb | None) -> None:
    """Stream ``url`` to ``dest``, reporting bytes via ``progress_cb``.

    Raises ``OSError`` if the server declared a ``Content-Length`` and the
    body came up short. A dropped connection mid-stream ends the read loop
    exactly like a clean EOF, so without this comparison a truncated
    tarball was written and returned as a SUCCESS. That is how a corrupt
    GE-Proton reaches the compat tools dir, and from there umu dies on the
    half-written ``toolmanifest.vdf``.
    ``OSError`` is deliberate: it is already what
    :func:`download_with_retry` catches, so a short read now costs a
    retry with backoff instead of a broken install.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30, context=ssl_ctx()) as resp, dest.open("wb") as out:
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


def download_with_retry(
    url: str,
    dest: Path,
    progress_cb: ProgressCb | None,
    attempts: int = 3,
) -> bool:
    """Download with exponential backoff (5/10/20s). True on success."""
    for attempt in range(1, attempts + 1):
        try:
            download(url, dest, progress_cb)
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


def extract(tarball: Path, dest: Path) -> bool:
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


def checksum_url(tarball_url: str) -> str:
    """The ``.sha512sum`` asset GE publishes beside ``tarball_url``.

    Both naming eras pair up by stem: ``<tag>.tar.gz`` / ``<tag>.sha512sum``
    and ``<tag>-x86_64.tar.gz`` / ``<tag>-x86_64.sha512sum``.
    """
    return tarball_url.removesuffix(".tar.gz") + ".sha512sum"


def _fetch_published_sha512(url: str) -> str | None:
    """The hex digest listed at ``url``; ``""`` if GE has none; None if unreachable.

    A 404 is "this release ships no checksum", which is not a reason to
    refuse a Proton the user needs. Any other failure is a network problem,
    and installing unverified bytes because the checksum host blinked would
    defeat the check.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx()) as resp:
            text = resp.read(4096).decode("ascii", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ""
        logger.warning("[ge_installer] checksum fetch failed: %s", e)
        return None
    except (urllib.error.URLError, OSError) as e:
        logger.warning("[ge_installer] checksum fetch failed: %s", e)
        return None
    parts = text.split()
    return parts[0].lower() if parts else ""


def file_sha512(path: Path) -> str:
    """Hex SHA-512 of ``path``. Raises ``OSError``."""
    digest = hashlib.sha512()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_sha512(tarball_url: str, tarball: Path) -> str | None:
    """The tarball's SHA-512 if it matches GE's published one, else None.

    Returns the digest unverified (with a warning) only when the release
    publishes no checksum at all; see :func:`_fetch_published_sha512`.
    """
    published = _fetch_published_sha512(checksum_url(tarball_url))
    if published is None:
        return None
    try:
        actual = file_sha512(tarball)
    except OSError as e:
        logger.warning("[ge_installer] could not hash %s: %s", tarball, e)
        return None
    if not published:
        logger.warning(
            "[ge_installer] %s has no published .sha512sum — installing unverified",
            tarball.name,
        )
        return actual
    if actual != published:
        logger.error(
            "[ge_installer] %s does not match GE's published SHA-512 "
            "(got %s…, expected %s…) — discarding it",
            tarball.name, actual[:16], published[:16],
        )
        return None
    return actual


def stage_release(
    tag: str, url: str, staging: Path, progress_cb: ProgressCb | None,
) -> str | None:
    """Download, verify and unpack ``url`` into ``staging``; return its SHA-512.

    The tarball is deleted before returning on every path, so a failed or
    finished install never leaves ~600 MB behind in the compat tools dir.
    """
    tarball = staging / f"{tag}.tar.gz"
    try:
        if not download_with_retry(url, tarball, progress_cb):
            return None
        digest = verified_sha512(url, tarball)
        if digest is None or not extract(tarball, staging):
            return None
        return digest
    finally:
        tarball.unlink(missing_ok=True)
