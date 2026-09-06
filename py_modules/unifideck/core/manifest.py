"""Per-game install manifest — build and write.

py_modules/unifideck/core/manifest.py

When Unifideck installs a game it drops a small JSON manifest
file (``.unifideck_manifest.json`` by default) inside the game's
directory, recording store + game_id + title + executable path.
Epic and Amazon write one at the end of a successful install.

Two public surfaces:

* ``GameManifest`` dataclass — the typed record;
* ``build_manifest`` / ``write_manifest`` — compose and persist.

**Who reads what this writes.** Nothing in this module — which is what
made ``write_manifest`` look write-only to audit §1.4 g, which proposed
deleting it. The reader is :mod:`unifideck.core.marker_sweep`, where the
file is ``_MANIFEST_MARKER``: ``iter_marked_dirs`` parses its ``store``
and ``store_id`` back out, and ``find_for_game`` / ``sweep_game`` /
``sweep_all`` use that as *proof Unifideck created this directory* before
deleting anything. That proof is the only thing that lets uninstall and
"Delete all data" clean up a game installed outside the default library
root — a custom folder or an SD card, which the store CLIs do not scan.
Drop the write and those installs are stranded on disk while uninstall
reports success. Keep the two modules' contract in mind together.

This module used to own a discovery pass as well (``discover_all``
and friends, walking every game root to re-attach installs by
emitting ``GAME_INSTALLED``). It had no callers — only
``vulture_whitelist`` entries — and its ``GAME_INSTALLED`` emit
was the sole emit site for an event two services subscribed to,
making both look alive while neither could ever run. Install
state now flows through ``DOWNLOAD_COMPLETE`` →
``ShortcutService.mark_installed`` → ``SHORTCUT_INSTALL_STATE_CHANGED``.

All disk I/O goes through ``asyncio.to_thread`` so writes don't
block the event loop on slow storage.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.utils.config_helpers import get_cfg

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)

DEFAULT_MANIFEST_FILENAME = ".unifideck_manifest.json"


@dataclass
class GameManifest:
    """One game's install manifest record.

    Attributes:
        unifideck_version: plugin version that wrote the
            manifest (used for forward-compatibility
            decisions during discovery).
        store: store identifier (``"epic"``, ``"gog"``, …).
        store_id: store-specific game id, used to call back
            into the store.
        title: human-readable title.
        executable_relative: path to the launcher .exe
            relative to the install directory.
        installed_at: ISO-8601 install timestamp.
        platform: ``"windows"`` / ``"linux"`` — drives the
            Proton-vs-native launch path.
    """

    unifideck_version: str
    store: str
    store_id: str
    title: str
    executable_relative: str
    installed_at: str
    platform: str = "windows"

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict.

        Explicit field-by-field copy (rather than
        ``dataclasses.asdict``) because the on-disk format
        is a stable wire contract — we want any new
        dataclass field to consciously decide whether to
        join the manifest.

        Returns:
            Seven-key dict ready for ``json.dump``.
        """
        return {
            "unifideck_version": self.unifideck_version,
            "store": self.store,
            "store_id": self.store_id,
            "title": self.title,
            "executable_relative": self.executable_relative,
            "installed_at": self.installed_at,
            "platform": self.platform,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GameManifest | None:
        """Build a ``GameManifest`` from a raw dict, or ``None`` on bad data.

        Tolerant by design: three fields
        (``unifideck_version``, ``store``, ``store_id``) are
        mandatory; ``KeyError`` on those returns ``None``.
        The rest fall back to safe defaults if missing.
        ``TypeError`` (non-dict input) also returns ``None``.

        Returns ``None`` rather than raising because
        discovery is best-effort — one malformed manifest
        shouldn't abort the whole scan.

        Args:
            data: parsed JSON dict.

        Returns:
            ``GameManifest`` instance, or ``None`` on bad
            data.
        """
        try:
            return cls(
                unifideck_version=data["unifideck_version"],
                store=data["store"],
                store_id=data["store_id"],
                title=data.get("title", ""),
                executable_relative=data.get("executable_relative", ""),
                installed_at=data.get("installed_at", ""),
                platform=data.get("platform", "windows"),
            )
        except (KeyError, TypeError):
            return None


def build_manifest(
    store: str,
    store_id: str,
    title: str,
    executable_relative: str,
    platform: str = "windows",
    unifideck_version: str = "1.0",
) -> GameManifest:
    """Construct a ``GameManifest`` with ``installed_at`` set to now.

    Convenience constructor that fills in the timestamp
    (UTC, ISO-8601) so callers don't have to import
    ``datetime`` themselves.

    Args:
        store: store identifier.
        store_id: store-specific game id.
        title: human-readable title.
        executable_relative: launcher .exe path relative to
            install dir.
        platform: ``"windows"`` or ``"linux"``.
        unifideck_version: plugin version stamping the
            manifest. Default ``"1.0"`` for legacy
            callers.

    Returns:
        Freshly-built ``GameManifest``.
    """
    return GameManifest(
        unifideck_version=unifideck_version,
        store=store,
        store_id=store_id,
        title=title,
        executable_relative=executable_relative,
        installed_at=datetime.now(UTC).isoformat(),
        platform=platform,
    )


def _cfg(config: ConfigManager | None, key: str, default: Any) -> Any:
    """Thin wrapper around ``get_cfg`` — local convention.

    Re-exports the shared config-reading helper under a
    short name to keep call sites compact. The wrapper
    has no extra logic — it exists purely for cosmetic
    readability inside this module.

    Args:
        config: optional ``ConfigManager``.
        key: dotted config key.
        default: fallback value.

    Returns:
        Config value or ``default``.
    """
    return get_cfg(config, key, default)


async def write_manifest(
    install_dir: str,
    store: str,
    store_id: str,
    title: str,
    executable_relative: str,
    platform: str = "windows",
    config: ConfigManager | None = None,
) -> bool:
    """Build + persist a manifest JSON file inside ``install_dir``.

    Calls ``build_manifest`` then writes the result to
    disk via a ``to_thread`` sync write. The filename is
    config-overridable via
    ``discovery.manifest_filename`` (defaults to
    ``.unifideck_manifest.json``).

    Args:
        install_dir: target directory (manifest written
            inside it).
        store / store_id / title / executable_relative /
            platform: forwarded to ``build_manifest``.
        config: optional ``ConfigManager`` (for filename
            override).

    Returns:
        ``True`` on successful write, ``False`` on
        ``OSError`` (logged at ERROR with the store +
        game_id context).
    """
    manifest = build_manifest(
        store,
        store_id,
        title,
        executable_relative,
        platform,
    )
    filename = get_cfg(
        config,
        "discovery.manifest_filename",
        DEFAULT_MANIFEST_FILENAME,
    )
    path = Path(install_dir) / filename

    def _write_sync() -> None:
        """Open + dump + close. Runs on a thread.

        Closure over ``path`` and ``manifest`` from the
        enclosing ``write_manifest``. Plain text-mode
        open + ``json.dump`` with 2-space indent for
        human-readable manifests (these may be inspected
        by users debugging an install).
        """
        with path.open("w", encoding="utf-8") as f:
            json.dump(manifest.to_dict(), f, indent=2)

    try:
        await asyncio.to_thread(_write_sync)
        logger.info(
            "[discovery] wrote manifest %s:%s → %s",
            store,
            store_id,
            path,
        )
        return True
    except OSError:
        logger.exception("[discovery] write_manifest %s:%s failed", store, store_id)
        return False
