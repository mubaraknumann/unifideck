"""services/download/prefix_warmup.py — install-time prefix initialisation.

Historically the Wine prefix was created lazily on the *first* game launch
(``ensure_prefix_initialized`` inside ``proton.dispatch``). That left a window
where the on-launch cloud-save sync-down ran *before* the prefix existed: the
save dir resolves out of ``drive_c`` (e.g. GOG's ``<?DOCUMENTS?>\\<title>``),
which isn't there until ``createprefix`` has run — so the first launch pulled no
saves and the user only saw them after a relaunch.

This module runs the SAME first-run setup eagerly at install time, for the
download stores that own a per-game prefix (Epic / GOG / Amazon — NOT Ubisoft,
which bootstraps its own prefix via UPC, nor Microsoft, which is cloud-only):

  1. ``ensure_prefix_initialized`` — ``umu-run createprefix`` (+ save migrate).
  2. ``apply_prefix_compat`` — winetricks redistributables + VC++ registry fix.
  3. ``CloudSaveService.sync_down`` — pull cloud saves now that ``drive_c`` exists.

Reuses the launch machinery (``proton_prepare`` etc.) by building a
``LaunchContext``/``ProtonLaunchPlan`` outside an actual launch. Every step is
idempotent (createprefix is skipped once ``system.reg`` exists, compat steps are
marker-guarded, gogdl skips an already-synced timestamp), so the launch-time
path re-running them later is a cheap no-op. Best-effort throughout: any failure
is logged and the install still completes — the launch-time path remains the
fallback.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import DownloadItem

logger = logging.getLogger(__name__)


def _build_launch_context(store: str, game_id: str, install: Path) -> Any:
    """Construct the ``(ctx, state)`` pair the setup steps run against.

    Builds a ``LaunchContext``/``RuntimeState`` outside an actual launch so
    the prefix-init / compat helpers can be reused at install time.
    """
    from unifideck.core.paths import resolve_plugin_dir
    from unifideck.launcher.types.context import LaunchContext, RuntimeState

    ctx = LaunchContext(
        store=store,
        game_id=game_id,
        # exe_path is unused by the setup steps (they key off WINEPREFIX); the
        # install dir is a harmless, valid Path placeholder.
        exe_path=install,
        work_dir=install,
        plugin_dir=resolve_plugin_dir(start=Path(__file__)),
        steam_app_id=None,
    )
    return ctx, RuntimeState()


def _reap_stale_prefix_wineserver(game_id: str) -> None:
    """Reap an orphaned wineserver on this game's prefix, if any.

    Best-effort pre-warmup hygiene (see call site). Non-Ubisoft install
    prefixes live at ``~/.local/share/unifideck/prefixes/<game_id>``; that
    is what the setup steps below build/use, so it's the server dir to
    clear. Any failure is swallowed — a broken reap must never block setup.
    """
    try:
        from unifideck.launcher.proton.infrastructure.wineserver_reap import (
            reap_prefix_wineserver,
        )
        prefix = Path(
            "~/.local/share/unifideck/prefixes",
        ).expanduser() / game_id
        if prefix.exists():
            reap_prefix_wineserver(prefix)
    except Exception:
        logger.exception(
            "[prefix_warmup] pre-warmup wineserver reap failed for %s", game_id,
        )


# Session vars the install-time umu runs need. The Decky backend is a
# headless service (spawned by plugin_loader) whose environment carries NO
# user session — none of these are set there. winetricks/vcredist under
# ntsync-era Proton builds (GE-Proton11+, Proton Exp 2026-07) hangs
# indefinitely or fails in that context, which wedged the serial install
# queue on every fresh-prefix install. Proven by A/B on-device: backend env →
# rc=1 / indefinite hang at vcredist_x86.EXE; backend env + these vars →
# rc=0 in ~55s. At launch time Steam provides them to the launcher (why the
# same winetricks always worked at launch, incl. all of 0.6.1); at install
# time we borrow them from the running Steam client so warmup matches launch.
#
# XAUTHORITY was the one this list originally missed, and its absence brought
# the hang straight back in a subtler form. plugin_loader runs as ROOT, so
# with DISPLAY=:0 grafted but no auth cookie every X connection is refused:
#
#     Authorization required, but no authorization protocol specified
#
# wine then blocks on that connection until the 120s compat-step killpg, so
# the step "times out" with no error of its own — twice per install, once for
# createprefix and once for regedit. Root can read the cookie despite its
# 0600 deck:deck mode, so grafting the path is sufficient; the value is a
# per-session file under XDG_RUNTIME_DIR (e.g. /run/user/1000/xauth_XXXXXX),
# which is why it has to be borrowed from Steam rather than guessed.
_SESSION_ENV_KEYS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "XAUTHORITY",
)


def _session_env_from_environ(data: bytes) -> dict[str, str]:
    """Extract the session vars from a ``/proc/<pid>/environ`` blob."""
    found: dict[str, str] = {}
    for chunk in data.split(b"\0"):
        key, sep, value = chunk.partition(b"=")
        if not sep or not value:
            continue
        try:
            name = key.decode()
        except UnicodeDecodeError:
            continue
        if name in _SESSION_ENV_KEYS:
            found[name] = value.decode(errors="replace")
    return found


def _session_env_from_steam_proc(uid: int) -> dict[str, str]:
    """Scan ``/proc`` for the running Steam client (same *uid*) and return
    the session env vars from its ``environ``, or ``{}`` if not found.

    Extracted from :func:`_user_session_env` to keep that function under
    the cognitive-complexity cap; behaviour is identical.
    """
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return {}
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            with open(f"/proc/{entry}/comm") as fh:
                if fh.read().strip() != "steam":
                    continue
            data = Path(f"/proc/{entry}/environ").read_bytes()
        except OSError:
            continue
        found = _session_env_from_environ(data)
        if found:
            return found
    return {}


def _user_session_env() -> dict[str, str]:
    """Best-effort user-session env for install-time umu runs.

    Borrows the four session vars from the running Steam client (same uid) —
    exactly the environment the launcher inherits at launch time. Falls back
    to the deterministic ``/run/user/<uid>`` locations when Steam isn't up.
    Returns only the vars it could resolve; callers merge with ``setdefault``
    so a launch-provided value is never clobbered.
    """
    uid = os.getuid()
    found = _session_env_from_steam_proc(uid)
    runtime = found.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    if os.path.isdir(runtime):
        found.setdefault("XDG_RUNTIME_DIR", runtime)
        if os.path.exists(f"{runtime}/bus"):
            found.setdefault(
                "DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus",
            )
    return found


async def _setup_prefix_with_ge_retry(ctx: Any, state: Any, key: str) -> None:
    """Run the canonical prefix setup at install time (headless).

    Thin adapter over :func:`launcher.proton.setup_prefix` — the ONE shared
    "createprefix + compat + managed-GE recovery + pin" process the first
    launch also runs. The only install-time-specific concern is grafting the
    user session env (the Decky backend is headless — see ``_SESSION_ENV_KEYS``
    and ``_user_session_env``); ``setup_prefix`` merges it with ``setdefault``
    so a launch-provided value is never clobbered.

    Best-effort: if every attempt still hangs, ``setup_prefix`` swallows it and
    the prefix finishes at first launch (which re-runs the identical steps).
    """
    from unifideck.launcher.proton import setup_prefix

    session_env = _user_session_env()
    logger.info(
        "[prefix_warmup] session env grafted for %s: %s",
        key, ", ".join(sorted(session_env)) or "none-resolved",
    )
    await setup_prefix(ctx, state, session_env=session_env)


async def warmup_install_prefix(
    store: str,
    game_id: str,
    install_path: str,
    *,
    cloud_svc: Any = None,
    proton_tool: str | None = None,
) -> None:
    """Run the full first-run prefix setup (+ cloud pull) at install time.

    ``proton_tool``: the Proton a fresh install is set up on (``pick_new_install_proton``).
    """
    from unifideck.launcher.frontend_bridge import suppress_launcher_toasts
    from unifideck.launcher.proton.store_setup import run_store_prefix_setup

    key = f"{store}:{game_id}"
    logger.info("[prefix_warmup] starting install-time prefix setup for %s", key)

    # Pre-warmup hygiene: reap any wineserver still holding THIS game's
    # prefix server dir, left orphaned by a prior hung/timed-out setup
    # attempt. Without this, the fresh createprefix below deadlocks against
    # the orphan on the shared /tmp/.wine-<uid>/server-<dev>-<ino>/lock (the
    # observed "install keeps getting stuck" wedge). Surgical — only this
    # prefix's server dir, never a wineserver for a game the user is running.
    _reap_stale_prefix_wineserver(game_id)

    ctx, state = _build_launch_context(store, game_id, Path(install_path))
    ctx.proton_tool = proton_tool

    # Steps 1+2 reuse the launch path's prefix-init / compat / GE-download /
    # umu-runtime helpers, all of which toast launch progress. During a
    # background install those toasts are noise (the download row shows
    # "Setting up game…"), so suppress them for the whole setup block.
    with suppress_launcher_toasts():
        await _setup_prefix_with_ge_retry(ctx, state, key)
        # Store-specific finishing (EA's Touchup). A failure here is retried
        # before the first launch, which also reports it.
        if not await run_store_prefix_setup(ctx, state, _user_session_env()):
            logger.warning("[prefix_warmup] store prefix step failed for %s; the launch retries it", key)

    # 3: pull cloud saves now that drive_c exists. The user opted into pulling
    # at install (in addition to the on-launch pull). Never fatal — a missing
    # store auth / network blip must not fail the install.
    if cloud_svc is not None:
        try:
            await cloud_svc.sync_down(store, game_id)
        except Exception:
            logger.exception(
                "[prefix_warmup] cloud sync_down failed for %s (non-fatal)", key,
            )

    logger.info("[prefix_warmup] finished install-time prefix setup for %s", key)


def make_prefix_warmup(
    cloud_svc: Any = None,
) -> Callable[[DownloadItem], Awaitable[None]]:
    """Build the download-worker hook bound to the cloud-save service.

    Returns a coroutine that takes the completed ``DownloadItem`` — the shape
    the worker's ``_prefix_warmup`` hook expects. The store-exclusion
    (Ubisoft / Microsoft) is enforced by the worker before this runs.
    """
    async def _warmup(item: DownloadItem) -> None:
        await warmup_install_prefix(
            item.store, item.game_id, item.install_path, cloud_svc=cloud_svc,
            proton_tool=item.proton_tool,
        )

    return _warmup
