from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.types.results import Result

from .argv_options import env_overrides_from, parse_argv, promote_env_tokens
from .types.context import LaunchContext
from .types.errors import GameNotFoundError, LauncherError
from .types.exit_codes import ExitCode
from .wrapper_prefix_probe import wrapper_prefix_is_populated
from .wrapper_stores import is_wrapper_store

if TYPE_CHECKING:
    from unifideck.services.shortcut import ShortcutService

logger = logging.getLogger(__name__)
def _resolve_plugin_dir() -> Path:
    """Resolve plugin dir."""
    from unifideck.core.paths import resolve_plugin_dir
    return resolve_plugin_dir(start=Path(__file__))
def _install_path_from_cache(store: str, game_id: str) -> str:
    """Look up an installed game's directory from the library cache.

    Fallback for games whose games.map row is missing entirely (e.g.
    installed by an older build that never wrote one). Returns ``""``
    when the cache is absent or the game isn't found.
    """
    cache = Path(
        "~/.local/share/unifideck/library_cache.json",
    ).expanduser()
    if not cache.is_file():
        return ""
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    libraries = data.get("libraries") if isinstance(data, dict) else None
    games = (libraries or {}).get(store) or []
    for game in games:
        if isinstance(game, dict) and game.get("store_game_id") == game_id:
            return str(game.get("install_path") or "")
    return ""


def _resolve_exe_from_install(store: str, install_path: str) -> str | None:
    """Re-resolve a launchable target from an install dir.

    Repairs stale/missing games.map rows at launch time. GOG (and
    other) Linux-native games launch via a root ``start.sh`` wrapper;
    everything else resolves the best ``.exe`` via the shared
    ``exe_finder`` heuristic. Best-effort — returns None on failure.

    Deliberately avoids importing any store package (e.g.
    ``GOGExeResolver``): the store ``__init__`` chains pull in auth →
    security → ``cryptography`` whose native ``_cffi_backend`` isn't
    importable in the slim launcher Python, which would crash the
    whole resolution. ``core.exe_finder`` is dependency-light and safe.
    """
    if not install_path:
        return None
    try:
        from unifideck.core.exe_finder import exe_finder

        # Native Linux games (notably GOG) launch via a ``start.sh``
        # wrapper at the install root — ``exe_finder`` only finds
        # ``.exe`` files, so check for it explicitly first.
        start_sh = Path(install_path) / "start.sh"
        if start_sh.is_file():
            return str(start_sh)
        return exe_finder.find(install_path)
    except Exception:
        logger.exception(
            "[launcher.dispatcher] exe re-resolution failed for %s",
            install_path,
        )
        return None


async def _build_context(
    argv: list[str],
    shortcut_svc: ShortcutService,
) -> LaunchContext:
    """Build context."""
    game_key, raw_options = parse_argv(argv)
    store, game_id = game_key.split(":", 1)

    # Steam passes launch options as argv, not as env vars — promote
    # any ``UNIFIDECK_*=value`` tokens so the rest of the dispatcher
    # can read them from ``os.environ`` as it expects.
    promote_env_tokens(raw_options)

    # Non-launch action path. Two kinds, both keyed by
    # ``UNIFIDECK_<STORE>_ACTION``:
    #   * ``auth``    — key ``<store>:<store>-auth``, opens sign-in.
    #   * ``install`` — wrapper stores only; key ``<store>:<game_id>``,
    #     opens the vendor client to install the title (via RunGame, so it
    #     gets a gamescope session in Gaming Mode). The game has no
    #     games.map row yet, so short-circuit the registry lookup like auth
    #     does.
    special = _special_action_context(store, game_id, raw_options, game_key)
    if special is not None:
        return special

    exe, work_dir, has_entry, app_id = await _resolve_game_exe(
        shortcut_svc, store, game_id, game_key,
    )
    if not exe:
        # Microsoft titles are Xbox Cloud Gaming (browser-streamed) —
        # no install and no games.map row. Synthesize the xCloud
        # context so the matrix routes to ``_launch_xcloud``.
        if store == "microsoft":
            logger.info(
                "[launcher.dispatcher] microsoft game not in games.map — "
                "synthesizing xCloud context for %s", game_key,
            )
            return _xcloud_context(store, game_id, raw_options)
        if is_wrapper_store(store):
            # An *installed* wrapper-store title legitimately has no resolvable
            # exe: its vendor client launches the game (Ubisoft via the
            # ``uplay://launch/{id}/0`` deeplink, Battle.net via
            # ``--exec="launch <FAMILY>"``) and neither handler reads
            # ``exe_path``. A games.map row is the "installed" signal — route it
            # to the play handler so Play launches the GAME, not the client.
            # Opening UPC for an already-installed game was the regression
            # where "Play" re-opened Ubisoft Connect.
            if has_entry:
                logger.info(
                    "[launcher.dispatcher] %s game %s installed "
                    "(games.map row, no exe) — launching via its client",
                    store, game_key,
                )
                return _game_context(
                    store, game_id, exe, work_dir, raw_options, app_id,
                )
            # Not installed (no row) but the prefix is bootstrapped: this is
            # the install, and the client has to be opened into it. Two ways to
            # arrive here — Steam dropped the ``UNIFIDECK_*_ACTION=install``
            # token so it looks like a plain launch, or the install is simply
            # still running and no row exists yet. The second is the normal
            # case for every wrapper store: the row is only written once the
            # game has actually downloaded.
            if wrapper_prefix_is_populated(store, game_id):
                logger.info(
                    "[launcher.dispatcher] %s game %s not in games.map "
                    "but has a populated prefix — treating as install action",
                    store, game_key,
                )
                return _wrapper_install_context(
                    store, game_id, raw_options, store,
                )
        raise GameNotFoundError(
            f"game {game_key!r} not found in games.map",
            context={"game_key": game_key},
        )
    return _game_context(
        store, game_id, exe, work_dir, raw_options, app_id,
    )


async def _resolve_game_exe(
    shortcut_svc: ShortcutService, store: str, game_id: str, game_key: str,
) -> tuple[str, str, bool, int]:
    """Resolve ``(exe, work_dir, has_entry, app_id)`` from games.map.

    Games installed by older builds (or discovered via manifest) may
    have an absent row (Amazon) or an empty exe (GOG ``start.sh``); we
    re-resolve from the install dir so they launch without a reinstall.

    ``has_entry`` reports whether a games.map row existed at all — the
    "installed" signal the caller needs to distinguish an installed
    Ubisoft title (deeplink launch, exe legitimately empty) from one
    that was never installed.

    ``app_id`` is the shortcut's Steam AppID (signed, as games.map stores
    it; ``0`` when unknown). It is what lets the launcher look up the
    user's Steam Force-Compat choice — see :func:`_game_context`.
    """
    entry = await shortcut_svc.get_entry_for_game_key(store, game_id)
    has_entry = entry is not None
    app_id = (entry.app_id if entry else 0) or 0
    exe = (entry.exe if entry else "") or ""
    work_dir = (entry.work_dir if entry else "") or ""
    if not exe and store != "microsoft":
        if not work_dir:
            work_dir = _install_path_from_cache(store, game_id)
        resolved = (
            _resolve_exe_from_install(store, work_dir) if work_dir else None
        )
        if resolved:
            logger.info(
                "[launcher.dispatcher] re-resolved exe for %s → %s",
                game_key, resolved,
            )
            exe = resolved
    return exe, work_dir, has_entry, app_id


def _special_action_context(
    store: str, game_id: str, raw_options: str, game_key: str,
) -> LaunchContext | None:
    """The non-launch context for this run, or ``None`` for a plain launch.

    Split out of ``_build_context`` to keep it under the line cap; the
    branch itself is unchanged.
    """
    action_store, action, is_launch_action = _detect_special_action()
    if is_launch_action:
        return None
    logger.info(
        "[launcher.dispatcher] %s action detected: store=%s game_key=%s",
        action, action_store, game_key,
    )
    if action == "install":
        return _wrapper_install_context(
            store, game_id, raw_options, action_store or store,
        )
    return _action_context(
        store, game_id, raw_options, action_store, action or "auth")


def _action_context(
    store: str, game_id: str, raw_options: str, auth_store: str | None,
    action: str = "auth",
) -> LaunchContext:
    """Context for an auth or storefront shortcut (no game target).

    Both mean "open a window for this store, there is no game here", so
    they share one builder; ``action`` is what ``_handle_auth_path``
    routes on. Parameterised rather than copied: a fifth near-identical
    ``LaunchContext`` builder would push this file over its LOC cap.
    """
    plugin_dir = _resolve_plugin_dir()
    return LaunchContext(
        store=store,
        game_id=game_id,
        exe_path=Path("/dev/null"),
        work_dir=plugin_dir,
        plugin_dir=plugin_dir,
        raw_options=raw_options,
        env_overrides=env_overrides_from(raw_options),
        is_launch_action=False,
        auth_store=auth_store,
        action=action,
        bypass_circuit_breaker=False,
    )


def _wrapper_install_context(
    store: str, game_id: str, raw_options: str, wrapper_store: str,
) -> LaunchContext:
    """Context for a wrapper store's ``install`` action.

    Like the auth context this is a non-launch action with no games.map row
    (the title isn't installed yet), but it carries the real ``game_id`` so
    the launcher resolves the per-game prefix recorded in the store's id map
    during bootstrap — plus, for Ubisoft, the ``uplay://install`` deeplink
    and, for Battle.net, the ``--exec`` family code. Routed by
    ``LauncherService._handle_auth_path`` on ``action == "install"``, which
    picks the handler off ``auth_store`` — so that field must name the
    wrapper store, not be hardcoded.
    """
    plugin_dir = _resolve_plugin_dir()
    return LaunchContext(
        store=store,
        game_id=game_id,
        exe_path=Path("/dev/null"),
        work_dir=plugin_dir,
        plugin_dir=plugin_dir,
        raw_options=raw_options,
        env_overrides=env_overrides_from(raw_options),
        is_launch_action=False,
        auth_store=wrapper_store,
        action="install",
        bypass_circuit_breaker=False,
    )


def _xcloud_context(
    store: str, game_id: str, raw_options: str,
) -> LaunchContext:
    """Synthesized xCloud context for a Microsoft title with no row."""
    plugin_dir = _resolve_plugin_dir()
    return LaunchContext(
        store=store,
        game_id=game_id,
        exe_path=Path("xcloud"),
        work_dir=plugin_dir,
        plugin_dir=plugin_dir,
        raw_options=raw_options,
        env_overrides=env_overrides_from(raw_options),
        is_launch_action=True,
        auth_store=None,
        bypass_circuit_breaker=False,
    )


def _game_context(
    store: str, game_id: str, exe: str, work_dir: str, raw_options: str,
    app_id: int = 0,
) -> LaunchContext:
    """Context for a normal installed game with a resolved exe.

    ``app_id`` (games.map's Steam AppID, signed) becomes
    ``ctx.steam_app_id`` — what ``select_proton_version`` needs to read the
    user's Steam Force-Compat choice. It was NEVER populated: the field
    defaulted to ``None`` and no builder set it, so that function's
    ``if steam_app_id`` guard skipped the tier on every launch and the
    user's Properties > Compatibility pick was never read for any game.
    """
    plugin_dir = _resolve_plugin_dir()
    return LaunchContext(
        store=store,
        game_id=game_id,
        exe_path=Path(exe),
        work_dir=Path(work_dir) if work_dir else plugin_dir,
        plugin_dir=plugin_dir,
        raw_options=raw_options,
        env_overrides=env_overrides_from(raw_options),
        is_launch_action=True,
        auth_store=None,
        steam_app_id=str(app_id) if app_id else None,
        bypass_circuit_breaker=_resolve_bypass_flag(store, game_id),
    )

def _detect_special_action() -> tuple[str | None, str | None, bool]:
    """Detect a non-launch action from ``UNIFIDECK_<STORE>_ACTION``.

    Returns ``(store, action, is_launch_action)``. ``auth`` and
    ``storefront`` are valid for every store; ``install`` is valid for the
    *wrapper* stores only, where it opens the vendor client so the user can
    start the download. Anything else (or no token) is a normal game launch
    (``(None, None, True)``).

    ``storefront`` is deliberately NOT wrapper-gated: every store has a
    shop. Which *kind* — web storefront or the vendor client's own Store
    tab — ``_handle_auth_path`` decides, ``is_wrapper_store`` first.

    The wrapper test goes through ``is_wrapper_store`` rather than a literal
    ``== "ubisoft"``: that literal is why ``UNIFIDECK_BATTLENET_ACTION=install``
    fell through to the normal-launch path, found no games.map row for a
    game that is by definition not installed yet, and raised
    ``GameNotFoundError`` — leaving ``battlenet_install_launch`` unreachable.
    """
    action_env = {
        "epic":      os.environ.get("UNIFIDECK_EPIC_ACTION"),
        "gog":       os.environ.get("UNIFIDECK_GOG_ACTION"),
        "amazon":    os.environ.get("UNIFIDECK_AMAZON_ACTION"),
        "microsoft": os.environ.get("UNIFIDECK_MICROSOFT_ACTION"),
        "ubisoft":   os.environ.get("UNIFIDECK_UBISOFT_ACTION"),
        "battlenet": os.environ.get("UNIFIDECK_BATTLENET_ACTION"),
    }
    for candidate_store, action in action_env.items():
        if action == "auth":
            return candidate_store, "auth", False
        if action == "storefront":
            return candidate_store, "storefront", False
        if action == "install" and is_wrapper_store(candidate_store):
            return candidate_store, "install", False
    return None, None, True
def _resolve_bypass_flag(store: str, game_id: str) -> bool:
    """Resolve bypass flag."""
    bypass_raw = os.environ.get(
        "UNIFIDECK_BYPASS_CIRCUIT_BREAKER", "",
    )
    bypass_env = bypass_raw.strip().lower() in (
        "1", "true", "yes",
    )
    try:
        from unifideck.config.config_manager import ConfigManager
        from unifideck.config.defaults_path import resolve_defaults_config_path
        from unifideck.services.launch_history import LaunchHistoryService
        # See ``launcher/bootstrap._load_standalone_config``: the nested
        # ``defaults/`` path does not exist on a Decky CLI build.
        cfg = ConfigManager(
            resolve_defaults_config_path(_resolve_plugin_dir()),
        )
        lh = LaunchHistoryService(cfg)
        bypass_flag = lh.consume_bypass(f"{store}:{game_id}")
    except Exception:
        bypass_flag = False
    return bypass_env or bypass_flag
async def _run(argv: list[str]) -> int:
    """Run."""
    from .diagnostics.correlation import launch_id_scope, new_launch_id
    from .diagnostics.log_archive import (
        attach_launch_handler,
        detach_launch_handler,
        prune_old_launches,
    )
    lid = new_launch_id()
    with launch_id_scope(lid):
        try:
            from unifideck.config.config_manager import ConfigManager
            from unifideck.core.paths import resolve_plugin_dir
            _cfg = ConfigManager(str(
                resolve_plugin_dir() /
                "defaults" /
                "config.json"))
        except Exception:
            _cfg = None
        prune_old_launches(_cfg)
        _archive_handler = attach_launch_handler(lid, _cfg)
        try:
            return await _run_with_id(argv)
        finally:
            detach_launch_handler(_archive_handler)

async def _run_with_id(argv: list[str]) -> int:

    """Run with ID."""
    try:
        game_key, _ = parse_argv(argv)
        logger.info(
            "[launcher.dispatcher] request received: %s", game_key,
        )
    except LauncherError as err:
        logger.exception(
            "[launcher.dispatcher] argv parse failed: %s",
            err.to_log_dict(),
        )
        return int(err.exit_code)
    try:
        launcher_service = _bootstrap_minimal_services()
    except Exception:
        logger.exception("[launcher.dispatcher] bootstrap failed")
        return int(ExitCode.DEPENDENCY_MISSING)
    try:
        ctx = await _build_context(argv, launcher_service._shortcut_svc)
    except LauncherError as err:
        logger.exception(
            "[launcher.dispatcher] context build failed: %s",
            err.to_log_dict(),
        )
        return int(err.exit_code)
    try:
        await launcher_service.start()
        result = await launcher_service.launch(ctx)
    except LauncherError as err:
        logger.exception(
            "[launcher.dispatcher] launch raised: %s",
            err.to_log_dict(),
        )
        return int(err.exit_code)
    finally:
        try:
            await launcher_service.stop()
        except Exception:
            logger.exception(
                "[launcher.dispatcher] launcher_service.stop failed",
            )
    return _map_result_to_exitcode(result)
def _map_result_to_exitcode(result: Result) -> int:
    """Map result to exitcode."""
    if result.success:
        return int(ExitCode.SUCCESS)
    code = result.error_code or ""
    if code == "not_implemented":
        return int(ExitCode.GENERIC_ERROR)
    if code == "circuit_open":
        return int(ExitCode.CIRCUIT_BREAKER_OPEN)
    if code.startswith("exit_"):
        try:
            rc = int(code.split("_", 1)[1])
            return rc if 0 <= rc <= 255 else int(ExitCode.GAME_FAILED)
        except (ValueError, IndexError):
            return int(ExitCode.GAME_FAILED)
    return int(ExitCode.GAME_FAILED)
def _bootstrap_minimal_services() -> Any:
    """Bootstrap minimal services.

    Returns the ``LauncherService`` built by
    ``launcher.bootstrap.build_launcher_service``. Typed as
    ``Any`` to match the source return type (the bootstrap
    helper itself is intentionally untyped at the call site to
    avoid circular imports).
    """
    from .bootstrap import build_launcher_service
    return build_launcher_service()

def main(argv: list[str]) -> int:

    """Main."""
    from .diagnostics.correlation import install_launch_id_logging
    logging.basicConfig(
        format=(
            "%(asctime)s [%(launch_id)s] %(levelname)s "
            "%(name)s: %(message)s"
        ),
        level=logging.INFO,
        stream=sys.stderr,
    )
    install_launch_id_logging()
    try:
        return int(asyncio.run(_run(argv)))
    except KeyboardInterrupt:
        return int(ExitCode.CANCELLED_BY_USER)
    except asyncio.CancelledError:
        logger.info(
            "[launcher.dispatcher] launch cancelled by user",
        )
        return int(ExitCode.CANCELLED_BY_USER)
    except Exception:
        logger.exception("[launcher.dispatcher] uncaught exception")
        return int(ExitCode.GENERIC_ERROR)
if __name__ == "__main__":
    sys.exit(main(sys.argv))
