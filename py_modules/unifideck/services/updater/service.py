"""services/updater/service.py — GitHub Releases version checker.

Fetches release metadata from the GitHub API for
``mubaraknumann/unifideck``, parses each release into a typed
dataclass, filters to only installable ``.zip`` assets, and
provides version-comparison helpers for the update UI.

Caches release data for 1 hour to avoid hammering the API.
A background polling task re-checks every 6 hours to keep that
cache warm and logs when a newer version is found; it does not
emit an event. The user-facing notice (a capped toast plus the
dot on the plugin's QAM icon) is raised frontend-side by
``src/services/pluginUpdateNotice.tsx``, which polls
``check_plugin_update`` off this same cache.

This service does NOT perform the actual installation — that's
handled by the frontend calling Decky Loader's built-in
``utilities/install_plugin`` route with the asset URL returned
by this service.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import aiohttp
import certifi

logger = logging.getLogger(__name__)


@dataclass
class ReleaseInfo:
    """Parsed GitHub release with installable asset metadata."""

    tag: str               # "Release-0.6.1"
    version: str           # "0.6.1" (parsed semver) or raw tag for non-semver
    name: str              # "UNIFIDECK v0.6.1"
    asset_url: str         # Direct browser_download_url for the .zip
    asset_name: str        # "unifideck.prod.v0.6.1.zip"
    sha256: str            # From the digest field, or "" if absent
    size_bytes: int        # Asset size in bytes
    prerelease: bool       # True for dev/staging builds
    published_at: str      # ISO 8601 timestamp
    body: str              # Release notes (markdown)
    download_count: int    # From asset metadata

    def to_dict(self) -> dict[str, Any]:
        """Serialise for RPC transport."""
        return asdict(self)


# Regex to extract a semver-like version from tag names.
# Handles: Release-0.6.1, Release_0.2.2, Release-0.5.0, Release1
_TAG_VERSION_RE = re.compile(
    r"(?:Release[-_]?)?"    # optional "Release-" or "Release_" prefix
    r"(\d+\.\d+(?:\.\d+)?)"  # capture X.Y or X.Y.Z
)


def _parse_version_from_tag(tag: str) -> str:
    """Extract a semver string from a GitHub tag name.

    Returns the numeric portion, zero-padded to X.Y.Z (e.g. tag
    ``"Release-0.7"`` becomes ``"0.7.0"``), so it compares equal to
    ``package.json``'s always-three-component version string — some
    older/two-component release tags (e.g. ``"Release-0.7"``) would
    otherwise never match the installed version and the UI's
    "(installed)" tag would never appear for them. Returns the raw tag
    if no semver pattern is found at all — which is exactly what dev
    release tags rely on (``"Dev"``, or the per-build
    ``"Dev-20260808-171205-47e6d28"`` that build-plugin.sh now creates).
    Dev tags are deliberately kept free of any ``X.Y`` substring so they
    land here rather than colliding with a real ``Release-X.Y.Z``.
    """
    m = _TAG_VERSION_RE.search(tag)
    if not m:
        return tag
    parts = m.group(1).split(".")
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts)


def _version_tuple(version: str) -> tuple[int, ...]:
    """Convert ``"0.6.1"`` to ``(0, 6, 1)`` for comparison.

    Non-numeric versions (any dev tag, e.g. ``"Dev"`` or
    ``"Dev-20260808-171205-47e6d28"``) return ``(0,)`` so they sort
    below any real release.
    """
    try:
        return tuple(int(p) for p in version.split("."))
    except ValueError:
        return (0,)


class UpdaterService:
    """GitHub Releases version checker and update metadata provider.

    Attributes:
        GITHUB_API_URL: API endpoint for release listing.
        CACHE_TTL_SECONDS: How long to cache release data (1 hour).
        POLL_INTERVAL_SECONDS: Background polling interval (6 hours).
    """

    GITHUB_API_URL = (
        "https://api.github.com/repos/mubaraknumann/unifideck/releases"
    )
    CACHE_TTL_SECONDS = 3600        # 1 hour
    POLL_INTERVAL_SECONDS = 21600   # 6 hours
    USER_AGENT = "Unifideck-Plugin-Updater/1.0"

    def __init__(self, bus: Any, package_json_path: str) -> None:
        """Initialise with an EventBus and path to package.json.

        Args:
            bus: The shared EventBus for emitting update events.
            package_json_path: Absolute path to the plugin's
                ``package.json`` — used to read the installed version.
        """
        self._bus = bus
        self._package_json_path = package_json_path
        self._cached_releases: list[ReleaseInfo] = []
        self._cache_timestamp: float = 0.0
        self._poll_task: asyncio.Task[None] | None = None
        self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    # ── Public API ──────────────────────────────────────────────

    def get_current_version(self) -> str:
        """Read the installed plugin version from package.json."""
        try:
            data = json.loads(Path(self._package_json_path).read_text())
            version = data.get("version", "0.0.0")
            return version if isinstance(version, str) else "0.0.0"
        except Exception:
            logger.warning("Could not read package.json for version")
            return "0.0.0"

    def get_current_build_id(self) -> str | None:
        """Read the dev-build identifier stamped at build time, if present.

        Populated only for local ``./build-plugin.sh dev`` builds (see
        ``dev_build.json``, written next to ``package.json`` by
        ``_write_dev_build_json()`` in build-plugin.sh). Returns ``None``
        for production installs, CI-built PR artifacts, and any install
        that predates this feature — purely additive.
        """
        build_id_path = Path(self._package_json_path).parent / "dev_build.json"
        try:
            data = json.loads(build_id_path.read_text())
            build_id = data.get("build_id")
            return build_id if isinstance(build_id, str) and build_id else None
        except Exception:
            return None

    async def fetch_releases(self, *, force: bool = False) -> list[ReleaseInfo]:
        """Fetch and cache installable releases from GitHub.

        Returns only releases that have at least one ``.zip`` asset
        attachment (not source archives). Results include prerelease
        builds. Cached for ``CACHE_TTL_SECONDS`` unless ``force``
        is True.
        """
        now = time.monotonic()
        if (
            not force
            and self._cached_releases
            and (now - self._cache_timestamp) < self.CACHE_TTL_SECONDS
        ):
            return self._cached_releases

        try:
            releases = await self._fetch_from_github()
            self._cached_releases = releases
            self._cache_timestamp = time.monotonic()
            return releases
        except Exception:
            logger.exception("Failed to fetch GitHub releases")
            # Return stale cache if available
            return self._cached_releases

    async def check_for_update(self, *, force: bool = False) -> dict[str, Any]:
        """Check whether a newer version is available.

        Returns a dict with::

            {
                "available": bool,
                "current": "0.6.1",
                "current_build_id": "0.7.1.g3f9a1c2" | None,
                "latest": {<ReleaseInfo fields>} | None,
            }

        The ``latest`` field is the newest *stable* release (non-
        prerelease). If no stable release is found, the newest
        prerelease is used instead. ``current_build_id`` is only
        populated for local dev builds (see ``get_current_build_id``);
        it's ``None`` for production installs.

        Args:
            force: bypass ``CACHE_TTL_SECONDS`` and re-fetch from
                GitHub. Used by the explicit "Check for Updates"
                action: every dev build publishes a new prerelease and
                deletes the previous one, so a warm cache can otherwise
                hand back a release that no longer exists.
        """
        current = self.get_current_version()
        current_build_id = self.get_current_build_id()
        releases = await self.fetch_releases(force=force)

        if not releases:
            return {
                "available": False,
                "current": current,
                "current_build_id": current_build_id,
                "latest": None,
            }

        # Prefer latest stable; fall back to latest prerelease
        stable = [r for r in releases if not r.prerelease]
        latest = stable[0] if stable else releases[0]

        current_tuple = _version_tuple(current)
        latest_tuple = _version_tuple(latest.version)
        available = latest_tuple > current_tuple

        return {
            "available": available,
            "current": current,
            "current_build_id": current_build_id,
            "latest": latest.to_dict(),
        }

    def get_release_for_version(self, version: str) -> ReleaseInfo | None:
        """Look up a cached release by version string."""
        for r in self._cached_releases:
            if r.version == version:
                return r
        return None

    # ── Background polling ──────────────────────────────────────

    async def start_polling(self) -> None:
        """Start the 6-hour background update check loop."""
        if self._poll_task is not None:
            return
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="updater-poll",
        )
        logger.info("[Updater] background polling started (every %ds)", self.POLL_INTERVAL_SECONDS)

    async def stop_polling(self) -> None:
        """Cancel the background polling task."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
            logger.info("[Updater] background polling stopped")

    async def _poll_loop(self) -> None:
        """Internal loop: check every POLL_INTERVAL_SECONDS."""
        while True:
            try:
                await asyncio.sleep(self.POLL_INTERVAL_SECONDS)
                result = await self.check_for_update()
                if result["available"]:
                    logger.info(
                        "[Updater] new version available: %s",
                        result["latest"]["version"] if result["latest"] else "?",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[Updater] poll cycle error")

    # ── GitHub API ──────────────────────────────────────────────

    async def _fetch_from_github(self) -> list[ReleaseInfo]:
        """Hit the GitHub Releases API and parse the response."""
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.USER_AGENT,
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(
                self.GITHUB_API_URL,
                headers=headers,
                ssl=self._ssl_ctx,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "[Updater] GitHub API returned %d", resp.status,
                    )
                    return self._cached_releases
                data = await resp.json()

        releases: list[ReleaseInfo] = []
        for entry in data:
            release = self._parse_release(entry)
            if release is not None:
                releases.append(release)

        # Sort by version descending (newest first)
        releases.sort(key=lambda r: _version_tuple(r.version), reverse=True)
        return releases

    @staticmethod
    def _parse_release(entry: dict[str, Any]) -> ReleaseInfo | None:
        """Parse a single GitHub release JSON object.

        Returns None if the release has no installable .zip asset
        (only source archives, or body-linked downloads).
        """
        assets = entry.get("assets", [])
        # Find the first .zip asset that is NOT a source archive
        zip_asset = None
        for asset in assets:
            name = asset.get("name", "")
            if name.endswith(".zip") and "source" not in name.lower():
                zip_asset = asset
                break

        if zip_asset is None:
            return None

        tag = entry.get("tag_name", "")
        version = _parse_version_from_tag(tag)

        # Extract SHA-256 from the digest field (format: "sha256:<hex>")
        digest = zip_asset.get("digest", "")
        sha256 = ""
        if digest.startswith("sha256:"):
            sha256 = digest[7:]

        return ReleaseInfo(
            tag=tag,
            version=version,
            name=entry.get("name", ""),
            asset_url=zip_asset.get("browser_download_url", ""),
            asset_name=zip_asset.get("name", ""),
            sha256=sha256,
            size_bytes=zip_asset.get("size", 0),
            prerelease=entry.get("prerelease", False),
            published_at=entry.get("published_at", ""),
            body=entry.get("body", ""),
            download_count=zip_asset.get("download_count", 0),
        )
