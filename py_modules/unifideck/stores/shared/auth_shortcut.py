"""Persistent auth shortcuts for wrapper stores.

py_modules/unifideck/stores/shared/auth_shortcut.py

Wrapper stores sign in by running their vendor client. In Desktop Mode the
client can be spawned directly, but **in Gaming Mode it must come from a
Steam shortcut** — a bare subprocess gets no gamescope session, so its
window never appears. That is why signing in works on the desktop and
silently fails on the deck without one of these.

Generic over the store: everything that differs is in
``AuthShortcutSpec``, so EA App is a spec rather than another module.

Two Steam behaviours drive the shape here:

* **Steam reads ``shortcuts.vdf`` only at startup.** A shortcut written
  this session is absent from Steam's in-memory app store, and ``RunGame``
  on its appid fails with "Game configuration unavailable". The frontend
  handles that with a temporary shortcut; this module just has to return a
  ``launcher_path`` so it can.
* **The appid must be derived, not invented** — ``generate_app_id`` is a
  CRC of launcher plus identity, and the same inputs must always give the
  same appid or the shortcut is orphaned on the next run.

**Ubisoft keeps its own implementation, and that is a decision rather than
a backlog item.** ``ubisoft/auth/shortcut.py`` + ``shortcut_ops.py`` carry
behaviour this module has no equivalent for: pruning legacy
``ubisoft:.template`` rows and stray ``upc.exe`` rows (declaring those
deletions to the shortcuts write guard by appid, because an empty ``Exe``
reads as the user's row and not ours), ``validate_auth_shortcut`` field
self-heal, compat-tool clearing, and shortcut-registry integration.
Porting all of that here would move ~580 lines into ``shared/`` for a
single consumer, on the store with the longest incident history in the
sign-in path. Battle.net is the only consumer by design; a *new* wrapper
store is still a spec rather than another module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unifideck.core.compat_bridge import to_unsigned

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuthShortcutSpec:
    """Everything store-specific about one wrapper store's auth shortcut."""

    store: str
    #: ``store:id`` written into LaunchOptions, e.g. ``battlenet:bnet-auth``.
    store_game_id: str
    #: Shortcut name in Steam, e.g. ``Battle.net``.
    display_name: str
    #: Env token telling the launcher this run is a sign-in.
    action_env: str
    #: Env token naming the prefix directory, e.g.
    #: ``UNIFIDECK_BATTLENET_PREFIX_NAME``. Required whenever the auth
    #: prefix is not named after the id in ``store_game_id`` — the launcher
    #: otherwise derives the prefix from ``ctx.game_id`` and signs the user
    #: in to an empty directory. Set both this and :attr:`prefix_name`.
    prefix_env: str | None = None
    #: Directory name of the auth prefix, e.g. ``.bnet-auth``.
    prefix_name: str | None = None
    #: Milliseconds the frontend should wait for Steam to register it.
    launch_wait_ms: int = 3000

    def launch_options(self, launcher_path: str) -> str:
        """LaunchOptions for the shortcut. Must be byte-stable.

        :func:`ensure_auth_shortcut` compares against this exactly, so any
        change here orphans existing shortcuts — which is why it repairs a
        row whose id matches but whose options differ, rather than leaving
        the user with a tile that launches the wrong thing.
        """
        del launcher_path
        options = f"{self.store_game_id} {self.action_env}=auth"
        if self.prefix_env and self.prefix_name:
            options += f" {self.prefix_env}={self.prefix_name}"
        return options


def launcher_path_for(plugin_dir: str | None) -> str:
    """Absolute path to the shortcut launcher binary."""
    base = Path(plugin_dir) if plugin_dir else Path(__file__).resolve().parents[3]
    return str(base / "bin" / "unifideck-launcher")


def _entry_matches(entry: Any, spec: AuthShortcutSpec) -> bool:
    """True when *entry*'s launch id is this store's auth shortcut.

    Identity only — deliberately no ownership check, because the one
    caller that merely *reads* (:func:`find_in_vdf`) must not miss a
    row: a false negative there makes ``ensure_auth_shortcut`` add a
    second auth tile rather than reuse the existing one. Callers that
    go on to *write* pair this with :func:`_is_repairable`.

    Tightened from the original ``spec.store_game_id in options``
    substring test to a canonical ``store:id`` compare, so a row that
    merely contains the token no longer matches. The compare stays on
    the canonical head only, never the whole string —
    :func:`repair_launch_options` exists precisely to fix rows whose
    tail has drifted, and matching the tail would make it unable to
    find its own repair targets.
    """
    from unifideck.services.shortcut.launch_options import get_full_id

    if not isinstance(entry, dict):
        return False
    options = str(entry.get("LaunchOptions") or "")
    return get_full_id(options) == spec.store_game_id


def _is_repairable(entry: dict[str, Any], launcher_path: str) -> bool:
    """True when it is safe to rewrite *entry*'s fields.

    Ours by the ``Exe`` gate, or a row with no ``Exe`` at all. The
    second case is the one this repair exists for — a bare row launches
    nothing, so it cannot be a working shortcut of the user's, and
    giving it our launcher is what makes it functional again.
    """
    from unifideck.services.shortcut.write_guard import is_ours

    if is_ours(entry, launcher_path):
        return True
    exe = entry.get("Exe") or entry.get("exe") or ""
    return not (exe.strip().strip('"') if isinstance(exe, str) else exe)


def find_in_vdf(shortcuts: dict[str, Any], spec: AuthShortcutSpec) -> int | None:
    """Existing appid for this auth shortcut, or None."""
    for entry in shortcuts.values():
        if _entry_matches(entry, spec):
            appid = entry.get("appid") if isinstance(entry, dict) else None
            if isinstance(appid, int):
                return appid
    return None


def repair_launch_options(
    shortcuts: dict[str, Any], spec: AuthShortcutSpec, launcher_path: str,
) -> bool:
    """Rewrite a matching row whose LaunchOptions have gone stale.

    A row is matched on ``store_game_id`` alone, so it survives a change to
    the rest of the string — adding the prefix-name token, for instance.
    Without this the old row keeps winning the ``find_in_vdf`` lookup and
    the shortcut goes on launching with the previous, wrong arguments; the
    user sees a tile that works and a sign-in that silently does nothing.

    Returns True when something was changed and the VDF needs writing.
    """
    expected = spec.launch_options(launcher_path)
    repaired = False
    for entry in shortcuts.values():
        if not _entry_matches(entry, spec):
            continue
        # The rewrite below is what needs ownership, not the lookup.
        if not _is_repairable(entry, launcher_path):
            logger.warning(
                "[%s] a shortcut carries our auth id but is not ours "
                "(Exe=%r) — leaving it alone",
                spec.store, entry.get("Exe"),
            )
            continue
        if str(entry.get("LaunchOptions") or "") == expected:
            continue
        logger.info(
            "[%s] repairing stale auth LaunchOptions: %r -> %r",
            spec.store, entry.get("LaunchOptions"), expected,
        )
        entry["LaunchOptions"] = expected
        repaired = True
    return repaired


def _build_entry(
    spec: AuthShortcutSpec, launcher_path: str, appid: int,
) -> dict[str, Any]:
    return {
        "appid": appid,
        "AppName": spec.display_name,
        "Exe": f'"{launcher_path}"',
        "StartDir": f'"{Path(launcher_path).parent}"',
        "LaunchOptions": spec.launch_options(launcher_path),
        # Hidden: it is an infrastructure tile, not a game the user browses.
        "IsHidden": 1,
        "AllowDesktopConfig": 1,
        "OpenVR": 0,
        "tags": {"0": spec.display_name},
    }


async def _read_shortcuts_from_disk(shortcut_service: Any) -> dict[str, Any]:
    """The VDF as it is on disk, not as the service last cached it.

    ``ShortcutService`` keeps ``shortcuts.vdf`` in memory for the process
    lifetime. Steam keeps its own copy and flushes it over ours, so a row we
    wrote this session can be gone from disk while the cache still reports
    it — measured on-device: an auth shortcut written at 01:39 was absent at
    01:58, and every later check answered "already in VDF", so nothing ever
    re-created it and the tile stayed missing from Steam.

    Falls back to the plain read when the service predates the keyword (test
    doubles, and any third-party shortcut service): a stale answer is worse
    than a fresh one but far better than an exception on the sign-in path.
    """
    try:
        data = await shortcut_service.read_shortcuts(from_disk=True)
    except TypeError:
        data = await shortcut_service.read_shortcuts()
    return dict(data) if isinstance(data, dict) else {"shortcuts": {}}


async def _emit_shortcut_created(
    bus: Any, spec: AuthShortcutSpec, unsigned: int,
) -> None:
    """Announce a freshly-written auth shortcut so ArtworkService can cover it.

    ``ArtworkService._on_shortcut_created`` filters on ``is_auth`` and maps the
    store id through its own ``_AUTH_TITLE_FOR_LOOKUP`` to a name SteamGridDB
    actually has art for, so the payload only has to carry identity. It wants
    the UNSIGNED appid: that is what Steam's ``grid/`` filenames use.

    This emit is why the handler exists. It sat subscribed and unreachable for
    the project's whole life (audit §1.3) — Ubisoft never needed it because it
    fetches its own auth artwork in ``ubisoft/auth/context.py``, and the four
    OAuth stores moved to ephemeral 15-second shortcuts that are gone before
    any art could matter. Battle.net is the one store that reaches this module
    and keeps a persistent tile, so its sign-in tile rendered bare.

    Best-effort: artwork is cosmetic and must never break a sign-in.
    """
    if bus is None:
        return
    from unifideck.core.types.events import Events
    try:
        await bus.emit(
            Events.SHORTCUT_CREATED,
            store=spec.store,
            app_id=unsigned,
            title=spec.display_name,
            is_auth=True,
        )
    except Exception:
        logger.debug(
            "[%s] SHORTCUT_CREATED emit failed — auth shortcut is still usable",
            spec.store, exc_info=True,
        )


async def ensure_auth_shortcut(
    shortcut_service: Any,
    spec: AuthShortcutSpec,
    plugin_dir: str | None,
    bus: Any = None,
) -> int | None:
    """Create or repair the persistent auth shortcut. Returns its unsigned appid.

    Never raises: a missing shortcut service or an unwritable VDF degrades
    to ``None``, and the frontend falls back to a temporary shortcut.

    ``bus`` is optional so test doubles and any caller that only wants the
    appid can omit it; when present, a NEWLY created shortcut emits
    ``SHORTCUT_CREATED`` for the artwork pipeline.
    """
    if shortcut_service is None:
        logger.debug("[%s] no shortcut service — cannot create auth shortcut", spec.store)
        return None

    launcher_path = launcher_path_for(plugin_dir)
    try:
        appid = shortcut_service.generate_app_id(launcher_path, spec.display_name)
        unsigned = to_unsigned(appid)

        data = await _read_shortcuts_from_disk(shortcut_service)
        shortcuts = data.get("shortcuts", {})

        existing = find_in_vdf(shortcuts, spec)
        if existing is not None:
            if repair_launch_options(shortcuts, spec, launcher_path):
                data["shortcuts"] = shortcuts
                await shortcut_service.write_shortcuts(data)
            logger.info("[%s] auth shortcut already in VDF (appid=%s)", spec.store, existing)
            return to_unsigned(existing)

        indices = [int(k) for k in shortcuts if str(k).isdigit()]
        shortcuts[str(max(indices, default=-1) + 1)] = _build_entry(
            spec, launcher_path, appid,
        )
        data["shortcuts"] = shortcuts
        await shortcut_service.write_shortcuts(data)
        logger.info("[%s] created auth shortcut in VDF (appid=%d)", spec.store, unsigned)
    except Exception:
        logger.exception("[%s] auth shortcut creation failed", spec.store)
        return None
    # Create branch only. The early return above covers "already in VDF", and
    # re-announcing an existing shortcut on every sign-in would re-run the SGDB
    # lookup for art that is already on disk.
    await _emit_shortcut_created(bus, spec, int(unsigned))
    return int(unsigned)


async def build_context(
    shortcut_service: Any,
    spec: AuthShortcutSpec,
    plugin_dir: str | None,
    bus: Any = None,
) -> dict[str, Any]:
    """The payload the frontend needs to RunGame this store's auth shortcut.

    ``launcher_path`` is always returned, even on failure, so the frontend
    can fall back to a temporary shortcut — which is the only thing that
    works during the first session after the VDF is written.

    Pass ``bus`` to have a newly created shortcut announce itself for artwork.
    """
    launcher_path = launcher_path_for(plugin_dir)
    unsigned = await ensure_auth_shortcut(
        shortcut_service, spec, plugin_dir, bus=bus,
    )
    if unsigned is None:
        return {
            "success": False,
            "error": "auth_shortcut_not_ready",
            "launcher_path": launcher_path,
        }
    return {
        "success": True,
        "appid_unsigned": unsigned,
        "launcher_path": launcher_path,
        "launch_wait_ms": spec.launch_wait_ms,
    }
