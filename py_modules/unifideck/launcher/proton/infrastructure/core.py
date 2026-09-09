"""launcher/proton/infrastructure/core.py — Shared Proton/UMU launch setup."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from unifideck.launcher.types.context import LaunchContext, RuntimeState
from unifideck.launcher.types.errors import DependencyMissingError

logger = logging.getLogger(__name__)

STORE_TO_UMU = {
    "epic": "egs",
    "gog": "gog",
    "amazon": "amazon",
    "ubisoft": "ubisoft",
    "microsoft": "microsoft",
}


def sanitize_frozen_loader_env(env: dict[str, str]) -> None:
    """Undo a PyInstaller-frozen parent's dynamic-loader pollution, in place.

    When the launch plan is built inside the Decky plugin process — whose
    ``PluginLoader`` is a PyInstaller-frozen binary — ``os.environ`` carries
    ``LD_LIBRARY_PATH=/tmp/_MEIxxxx`` pointing at the loader's *bundled* libs
    (an old ``libcrypto`` lacking ``OPENSSL_3.3.0``). umu-run runs under the
    SYSTEM python, so that path makes its ``import ssl`` fail with
    ``ImportError: libcrypto.so.3: version 'OPENSSL_3.3.0' not found`` — umu
    then aborts, so ``createprefix`` / winetricks silently do nothing (the
    install-time prefix warmup produced empty prefixes for exactly this
    reason). PyInstaller stashes the real pre-launch value in
    ``LD_LIBRARY_PATH_ORIG``, but that value is no more welcome than the
    ``_MEI`` one — see below — so it is discarded rather than restored.

    BOTH loader variables are write-once-never on the Proton/umu path:
    discard any ``*_ORIG`` instead of restoring from it, and drop a
    ``_MEI``-tainted live value.

    ``LD_PRELOAD`` — all umu-run launches go through pressure-vessel (a
    container) which has its own Steam overlay mechanism; re-exporting the
    host's ``gameoverlayrenderer.so`` causes "cannot be preloaded" errors and
    can crash/early-exit the game process (``WARNING: Keyboard Interrupt``).

    ``LD_LIBRARY_PATH`` — umu copies it into ``STEAM_RUNTIME_LIBRARY_PATH``,
    carrying a *host* library path into the container where it shadows the
    container's own libs; the container then fails to start ``python3`` (the
    interpreter of Proton's launch script) with "error while loading shared
    libraries: libz.so.1" and umu exits 127. Restoring ``_ORIG`` here also
    directly contradicted ``bin/unifideck-launcher``, which pops
    ``LD_LIBRARY_PATH`` at process start precisely because Steam's value
    breaks non-Steam binaries — it just never popped the ``_ORIG`` twin, so
    this function silently handed the value back. In the field that showed up
    as every GOG/Amazon/Ubisoft launch dying at 127 while Epic — which strips
    both vars at its own ``--wrapper`` boundary — kept working. (Where the
    inherited value originates is not established; it was seen on a plain
    Steam stable client, so a containerised Steam is not the explanation.
    The remedy is the same either way.)

    The retired bash launcher unset both once at startup and never restored
    them for any Proton/umu launch; mirror that here.
    """
    env.pop("LD_LIBRARY_PATH_ORIG", None)
    if "/_MEI" in env.get("LD_LIBRARY_PATH", ""):
        env.pop("LD_LIBRARY_PATH", None)

    env.pop("LD_PRELOAD_ORIG", None)
    if "/_MEI" in env.get("LD_PRELOAD", ""):
        env.pop("LD_PRELOAD", None)


@dataclass(frozen=True)
class ProtonLaunchPlan:
    """Everything store handlers need to spawn umu-run."""
    context: LaunchContext
    state: RuntimeState
    python_bin: Path
    umu_wrapper: Path
    prefix_path: Path
    env: dict[str, str]
    on_process_start: Callable[[object], None] | None = None
def _ubisoft_prefix_path(ctx: LaunchContext, prefixes_dir: Path) -> Path:
    """Ubisoft prefix path.

    Games can be installed to a user-picked location (SD / custom); the
    backend records the absolute per-game prefix path in
    ``ubisoft_id_map.json`` (the same file ``_uplay_id_from_id_map`` reads).
    Prefer that; fall back to the fixed internal location for games installed
    before this existed and for the auth shortcut (whose game_id has no
    recorded prefix — it uses ``UNIFIDECK_UBISOFT_PREFIX_NAME=.upc-auth``).
    """
    import json
    import os
    id_map_file = Path("~/.local/share/unifideck/ubisoft_id_map.json").expanduser()
    try:
        data = json.loads(id_map_file.read_text(encoding="utf-8"))
        entry = data.get(ctx.game_id) if isinstance(data, dict) else None
        recorded = entry.get("prefix_path") if isinstance(entry, dict) else None
        if recorded:
            return Path(recorded)
    except (OSError, ValueError):
        pass
    ubi_name = os.environ.get("UNIFIDECK_UBISOFT_PREFIX_NAME") or ctx.game_id
    return prefixes_dir / "ubisoft" / ubi_name
def _battlenet_prefix_path(ctx: LaunchContext, prefixes_dir: Path) -> Path:
    """Per-game Battle.net prefix, read from the id map — never rebuilt.

    A reconstructed path once stamped a marker into a directory no launch
    opened, wedging a Ubisoft prefix in a permanent reset loop. It also lets
    a game live on an SD card without the resolver guessing.
    """
    try:
        from unifideck.launcher.proton.handlers.battlenet_client import resolve_prefix

        recorded = resolve_prefix(ctx.game_id)
        if recorded is not None:
            return recorded
    except (OSError, ValueError, ImportError):
        pass
    override = os.environ.get("UNIFIDECK_BATTLENET_PREFIX_NAME") or ctx.game_id
    return prefixes_dir / "battlenet" / override


def _resolve_prefix(ctx: LaunchContext) -> Path:
    """Resolve prefix."""
    prefixes_dir = Path("~/.local/share/unifideck/prefixes").expanduser()
    if ctx.store == "ubisoft":
        path = _ubisoft_prefix_path(ctx, prefixes_dir)
    elif ctx.store == "battlenet":
        path = _battlenet_prefix_path(ctx, prefixes_dir)
    else:
        path = prefixes_dir / ctx.game_id
        while path.name == "pfx":
            path = path.parent
    path.mkdir(parents=True, exist_ok=True)
    return path
def _lookup_umu_id(
 ctx: LaunchContext,
 umu_store: str,
 plugin_dir: Path,
) -> str | None:
    """Lookup UMU ID."""
    helper = plugin_dir / "bin" / "umu_lookup.py"
    if not helper.is_file():
        return None
    try:
        out = subprocess.check_output(
            ["python3", str(helper), ctx.game_id, umu_store],
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        text = out.decode().strip()
        return text or None
    except (subprocess.SubprocessError, OSError):
        return None

def _locate_umu_wrapper(proton_path: Path, plugin_dir: Path) -> Path:

    """Locate UMU wrapper.

    Priority matches staging's launcher: the plugin-bundled zipapp at
    ``<plugin>/bin/umu/umu/umu-run`` (the canonical location on Deck
    installs), then any copy beside Proton, then a system ``umu-run``.
    """
    plugin_bundled = plugin_dir / "bin" / "umu" / "umu" / "umu-run"
    if plugin_bundled.is_file():
        return plugin_bundled
    bundled = proton_path.parent / "umu-run"
    if bundled.is_file():
        return bundled
    system = shutil.which("umu-run")
    if system:
        return Path(system)
    raise DependencyMissingError(
        "umu-run not found (not bundled at "
        "<plugin>/bin/umu/umu/umu-run, beside proton, nor in PATH)",
        context={
            "proton_path": str(proton_path),
            "plugin_dir": str(plugin_dir),
        },
    )
def _epic_store_value(
    game_id: str, umu_id: str | None, exe_name: str | None = None,
) -> str:
    """The ``STORE`` env value for an Epic launch.

    ``none`` for ordinary Epic titles (ProtonFixes' ``egs`` defaults add a
    ``HKCR\\com.epicgames.launcher`` key that flips the EOS SDK to
    launcher-IPC auth → instant exit/hang for EOS titles; the retired bash
    launcher forced ``none`` for exactly this). Rockstar-on-Epic (RDR2/GTA5)
    is the lone exception: it WANTS the ``egs`` profile — its protonfixes +
    that same launcher handler are what boot the Rockstar launcher. Gated on
    the Epic app name/exe name (``is_rockstar_egs``) so no ordinary Epic
    launch is affected.
    """
    from unifideck.launcher.proton.fixes.game_fixes import is_rockstar_egs
    return "egs" if is_rockstar_egs(game_id, umu_id, exe_name) else "none"


def _apply_battlenet_env(env: dict[str, str]) -> None:
    """Environment the Battle.net client needs, on every invocation.

    ``WINE_SIMULATE_WRITECOPY=1`` — without it the login window is
    unresponsive (confirmed on-device).

    ``PROTON_USE_XALIA=0`` — precautionary. Note the July 2026 study
    recommended ``PROTON_DISABLE_XALIA=1``; that variable **does not exist**
    in the Proton script and was always a no-op. Xalia ran throughout a
    later on-device session and the client was healthy, so this is belt and
    braces rather than the gating fix it was believed to be.

    ``DISABLE_GAMESCOPE_WSI=1`` — **only on a host that has already been
    measured to need it**, which is why it comes from a recorded marker
    rather than being set unconditionally. See
    :mod:`~unifideck.launcher.proton.handlers.battlenet_wsi`: the client's
    ANGLE renderer aborts inside gamescope's Vulkan WSI layer on some GPUs
    and not others, and turning the layer off costs the XWayland-bypass
    path (direct scanout, HDR) for the game too, since the game inherits
    the client's environment. Applying it everywhere would charge that to
    every working host to fix a minority of them.

    ``locationapi=d`` is **merged** into any existing WINEDLLOVERRIDES
    rather than replacing it — Proton appends its own long default list.
    """
    from unifideck.launcher.proton.handlers import battlenet_wsi

    env["WINE_SIMULATE_WRITECOPY"] = "1"
    env["PROTON_USE_XALIA"] = "0"
    battlenet_wsi.apply_if_recorded(env)
    existing = env.get("WINEDLLOVERRIDES", "")
    if "locationapi" not in existing:
        env["WINEDLLOVERRIDES"] = (
            f"locationapi=d;{existing}" if existing else "locationapi=d"
        )


def _apply_rockstar_dll_overrides(env: dict[str, str], umu_id: str | None) -> None:
    """Add the RDR2/GTA5 WINEDLLOVERRIDES to ``env`` in place.

    Native-then-builtin vulkan-1 is the documented Rockstar-on-Epic launch
    fix (Heroic). Merges with any existing overrides rather than clobbering;
    a user's explicit ``ctx.env_overrides`` still wins (applied afterwards).
    """
    from unifideck.launcher.proton.fixes.game_fixes import (
        ROCKSTAR_WINEDLLOVERRIDES,
    )
    existing = env.get("WINEDLLOVERRIDES", "")
    env["WINEDLLOVERRIDES"] = (
        f"{existing};{ROCKSTAR_WINEDLLOVERRIDES}"
        if existing else ROCKSTAR_WINEDLLOVERRIDES
    )
    logger.info(
        "[launcher.proton.core] Rockstar-EGS (%s): STORE=egs, "
        "WINEDLLOVERRIDES+=%s", umu_id, ROCKSTAR_WINEDLLOVERRIDES,
    )


def _apply_icu_dll_overrides(env: dict[str, str], game_id: str | None) -> None:
    """Add the native-ICU WINEDLLOVERRIDES to ``env`` in place.

    Merges with any existing overrides rather than clobbering (Proton
    appends its own long default list, and battlenet may already have put
    ``locationapi=d`` here); a user's explicit ``ctx.env_overrides`` still
    wins, since that is applied afterwards.

    The guard is PER DLL. It used to be a single ``"icuuc" in existing``
    test, which was fine while the constant named one library; now that it
    names both ``icuuc`` and ``icuin``, that test would see a user override
    mentioning only ``icuuc`` and skip ``icuin`` along with it — exactly the
    half-applied state that let Cyberpunk 2077 abort on ``icuin.dll``.
    """
    from unifideck.launcher.proton.fixes.game_fixes import ICU_NATIVE_DLLS
    existing = env.get("WINEDLLOVERRIDES", "")
    missing = [dll for dll in ICU_NATIVE_DLLS if dll not in existing]
    if not missing:
        return
    added = ";".join(f"{dll}=n,b" for dll in missing)
    env["WINEDLLOVERRIDES"] = f"{existing};{added}" if existing else added
    logger.info(
        "[launcher.proton.core] native-ICU title (%s): WINEDLLOVERRIDES+=%s",
        game_id, added,
    )


def _apply_per_title_env(
    env: dict[str, str],
    ctx: LaunchContext,
    *,
    umu_id: str | None,
    exe_name: str,
    rockstar_egs: bool,
) -> None:
    """Layer the per-title / per-store env quirks onto ``env`` in place.

    Split out of :func:`_build_umu_env` to keep it under the line cap. Every
    branch here is gated so a title that matches nothing is left byte-for-byte
    unchanged, and all of them run BEFORE ``ctx.env_overrides`` so a user's
    explicit value still wins.
    """
    from unifideck.launcher.proton.fixes.game_fixes import needs_native_icu
    if rockstar_egs:
        _apply_rockstar_dll_overrides(env, umu_id)
    if ctx.store == "battlenet":
        _apply_battlenet_env(env)
    # Store-independent: keyed off the exe/id, not ctx.store, because the
    # same title ships the same bundled ICU on GOG and Epic alike.
    if needs_native_icu(ctx.game_id, umu_id, exe_name):
        _apply_icu_dll_overrides(env, ctx.game_id)


def _apply_compat_paths(
    env: dict[str, str],
    *,
    prefix_path: Path,
    proton_path: Path,
    work_dir: Path,
) -> None:
    """Point umu-run at the Proton build, the prefix and the install dir.

    Split out of :func:`_build_umu_env` to keep it under the line cap. Every
    comment here is load-bearing — each records a specific field bug this
    exact layout fixes — so read before reordering.
    """
    # PROTONPATH tells umu-run which Proton to use — the *directory*
    # holding the ``proton`` script (``proton_path`` is that script, so
    # use its parent). Without this umu falls back to downloading its
    # own UMU-Proton (or fails), ignoring the tool we selected. Mirrors
    # staging's ``export PROTONPATH``.
    env["PROTONPATH"] = str(proton_path.parent)
    env["STEAM_COMPAT_DATA_PATH"] = str(prefix_path)
    # Pin the game to its per-game prefix. umu-run does NOT derive the
    # prefix from STEAM_COMPAT_DATA_PATH — with no WINEPREFIX it defaults
    # to ``~/Games/umu/$GAMEID`` (e.g. the shared ``umu-0`` when a game
    # has no per-game umu_id). That shared prefix lacks the deps our
    # compat steps install into prefix_path (they set WINEPREFIX
    # explicitly) AND it's not where cloud-save sync writes — so the game
    # would launch in the wrong prefix and never see its saves/deps.
    # Mirrors the compat steps (e.g. compat/winetricks.py).
    env["WINEPREFIX"] = str(prefix_path)
    # Game install dir — some Proton features/protonfixes key off this.
    env["STEAM_COMPAT_INSTALL_PATH"] = str(work_dir)
    # Let DXVK-NVAPI work on non-NVIDIA / mixed driver setups (harmless
    # on the Deck's AMD GPU; required by some titles' NVAPI probes).
    env["DXVK_NVAPI_ALLOW_OTHER_DRIVERS"] = "1"
    # NOTE: the Steam identity block (SteamGameId / STEAM_COMPAT_APP_ID /
    # SteamAppId / UMU_STEAM_GAME_ID) is NOT set here. ``build_steam_window_env``
    # in the caller is its one implementation. An earlier version of this merge
    # also re-exported the host's ``gameoverlayrenderer.so`` through LD_PRELOAD
    # to "fix" window binding; that is the exact thing ``sanitize_frozen_loader_env``
    # exists to prevent — see this module's LD_PRELOAD note above.

    # Do NOT pin STEAM_COMPAT_CLIENT_INSTALL_PATH. umu-run derives it
    # itself; forcing it to ``~/.steam/root`` — a symlink chain on
    # atomic/ostree hosts (Bazzite: ``~`` → /var/home, ``.steam/root`` →
    # steam) — makes pressure-vessel's ``/run/host`` + ``from-host``
    # capsule-capture loop when bwrap resolves ``pv-adverb`` → "Too many
    # levels of symbolic links" (ELOOP), so the game exits code 1 before it
    # starts (Cyberpunk/GOG on Bazzite). The pre-refactor bash launcher
    # deliberately UNSET this and worked on Deck + Bazzite + CachyOS; mirror
    # that — also drop any value Steam passed down, since that's the looping
    # one on atomic hosts.
    env.pop("STEAM_COMPAT_CLIENT_INSTALL_PATH", None)
    env["PROTON_VERB"] = "waitforexitandrun"


def _build_umu_env(
    ctx: LaunchContext,
    *,
    umu_store: str,
    umu_id: str | None,
    prefix_path: Path,
    proton_path: Path,
    proton_tool_id: str,
) -> dict[str, str]:
    """Build the umu-run environment for a Proton launch.

    Extracted from :func:`proton_prepare` to keep that function under the
    line cap; the ordering and every comment below are load-bearing (each
    documents a specific field bug this env layout fixed).
    """
    import os

    from unifideck.core.compat_bridge import to_unsigned
    from unifideck.launcher.proton.fixes.game_fixes import is_rockstar_egs
    from unifideck.steam.window_env import build_steam_window_env
    env = dict(os.environ)
    had_ld_preload_orig = "LD_PRELOAD_ORIG" in env
    # Strip the Decky PluginLoader's PyInstaller LD_LIBRARY_PATH pollution so
    # umu-run (system python) doesn't load a stale libcrypto and abort — the
    # cause of empty install-time prefixes. No-op for the clean launcher env.
    sanitize_frozen_loader_env(env)
    # A game with no umu-db entry used to fall back to the shared ``umu-0``,
    # which makes every such game one session as far as umu is concerned.
    # Fall back to this shortcut's own appid instead so the session is
    # distinct. ``to_unsigned`` because games.map stores the signed 32-bit
    # form and umu's GAMEID regex (``^umu-[\d\w]+$``) rejects the minus sign.
    # Prefixes are unaffected either way: WINEPREFIX is pinned below.
    unsigned_id: str | None = None
    if ctx.steam_app_id:
        try:
            unsigned_id = str(to_unsigned(ctx.steam_app_id))
        except (TypeError, ValueError):
            unsigned_id = None
    env["GAMEID"] = umu_id or (f"umu-{unsigned_id}" if unsigned_id else "umu-0")
    # See _epic_store_value for the Epic STORE reasoning. ``umu_store_code``
    # on state keeps the real value for diagnostics regardless of this.
    exe_name = ctx.exe_path.name
    rockstar_egs = ctx.store == "epic" and is_rockstar_egs(
        ctx.game_id, umu_id, exe_name,
    )
    if ctx.store == "epic":
        env["STORE"] = _epic_store_value(ctx.game_id, umu_id, exe_name)
    else:
        env["STORE"] = umu_store
    _apply_compat_paths(
        env,
        prefix_path=prefix_path,
        proton_path=proton_path,
        work_dir=ctx.work_dir,
    )
    # Tell umu which Steam app this is. Without it umu defaults the identity
    # to 0, gamescope reports ``steam app id: 0`` for the game's window and
    # the Deck session never adopts it — the launch screen sits on top of a
    # game that is already running. See unifideck.steam.window_env.
    env.update(
        build_steam_window_env(ctx.steam_app_id, log_tag="launcher.proton.core"),
    )
    _apply_per_title_env(
        env, ctx, umu_id=umu_id, exe_name=exe_name, rockstar_egs=rockstar_egs,
    )
    # LAST, so a user's explicit LaunchOptions env still beats everything
    # above — including the identity block.
    env.update(ctx.env_overrides)
    logger.info(
        "[launcher.proton.core] plan ready: store=%s umu_store=%s "
        "umu_id=%s prefix=%s proton=%s steam_app_id=%s ld_preload=%r "
        "had_ld_preload_orig=%s",
        ctx.store, umu_store, umu_id, prefix_path, proton_tool_id,
        env.get("SteamAppId"), env.get("LD_PRELOAD"), had_ld_preload_orig,
    )
    return env


def proton_prepare(
 ctx: LaunchContext,
 state: RuntimeState,
 *,
 python_bin: Path,
 proton_path: Path,
 proton_tool_id: str,
 on_process_start: Callable[[object], None] | None = None,
) -> ProtonLaunchPlan:
    """Proton prepare."""
    umu_store = STORE_TO_UMU.get(ctx.store, "none")
    prefix_path = _resolve_prefix(ctx)
    umu_id = _lookup_umu_id(ctx, umu_store, ctx.plugin_dir)
    umu_wrapper = _locate_umu_wrapper(proton_path, ctx.plugin_dir)
    state.python_bin = python_bin
    state.proton_path = proton_path
    state.proton_tool_id = proton_tool_id
    state.prefix_path = prefix_path
    state.umu_store_code = umu_store
    state.umu_id = umu_id
    state.umu_wrapper = umu_wrapper
    env = _build_umu_env(
        ctx, umu_store=umu_store, umu_id=umu_id,
        prefix_path=prefix_path, proton_path=proton_path,
        proton_tool_id=proton_tool_id,
    )
    # Start the gamescope window tagger before returning, not from the
    # on_process_start callback, so a window that appears before umu is fully
    # registered is still tagged. umu's own ``monitor_windows`` only runs when
    # ``container=flatpak``; under pressure-vessel it is skipped, so nothing
    # sets STEAM_GAME on the window.
    #
    # Gaming Mode only: outside a gamescope session there is no compositor to
    # tell, and the thread would poll an X display for five minutes for
    # nothing. ``build_steam_window_env`` above is what fixes window adoption
    # in the ordinary case — if the device check confirms it is sufficient on
    # its own, this whole module goes away.
    if os.environ.get("GAMESCOPE_WAYLAND_DISPLAY"):
        try:
            appid_int = int(env.get("SteamGameId") or 0)
        except ValueError:
            appid_int = 0
        if appid_int > 0:
            from .gamescope_window_tagger import start_window_tagger
            start_window_tagger(appid_int)

    return ProtonLaunchPlan(
        context=ctx,
        state=state,
        python_bin=python_bin,
        umu_wrapper=umu_wrapper,
        prefix_path=prefix_path,
        env=env,
        on_process_start=on_process_start,
    )
