from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from unifideck.core.compat_bridge import to_unsigned
from unifideck.launcher.types.errors import (
    DependencyMissingError,
    ProtonUnavailableError,
)
from unifideck.utils import vdf_compat

from . import external_ge, ge_installer

logger = logging.getLogger(__name__)
# Interpreters tried (in order) to run the umu zipapp. umu needs Python
# >=3.10 and uses its OWN bundled deps, so it does NOT depend on our vendored
# ``_cffi_backend`` — unlike the launcher process itself, which Steam starts
# via ``bin/unifideck-launcher``'s ``#!/usr/bin/env python3`` shebang and whose
# cryptography import DOES need a matching backend. That coverage is handled at
# build time: keep ACCEPTED_VERSIONS in sync with LAUNCHER_PYTHON_VERSIONS in
# build-plugin.sh so a backend is shipped for every host Python we accept.
PYTHON_CANDIDATES: list[str] = [
    "/usr/bin/python3.14",
    "/usr/bin/python3.13",
    "/usr/bin/python3.12",
    "/usr/bin/python3.11",
    "/usr/bin/python3.10",
    "/usr/bin/python3",
]
ACCEPTED_VERSIONS = {"3.10", "3.11", "3.12", "3.13", "3.14"}
def find_python_3_10_plus() -> Path:
    """Find python 3 10 plus."""
    for candidate in PYTHON_CANDIDATES:
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            out = subprocess.check_output(
                [
                    candidate,
                    "-c",
                    'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")',
                ],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (subprocess.SubprocessError, OSError):
            continue
        ver = out.decode().strip()
        if ver in ACCEPTED_VERSIONS:
            logger.info("[launcher.proton] python selected: %s (%s)", candidate, ver)
            return path
    raise DependencyMissingError(
        "No Python 3.10+ interpreter found on system",
        context={"tried": PYTHON_CANDIDATES},
    )

# ``~/.steam/root`` is a symlink Steam creates to the active install; on most
# distros it points at ``~/.local/share/Steam``. We also list ``~/.steam/steam``
# explicitly so Proton still resolves if that symlink is absent (fresh install,
# unusual setup) — it costs nothing when the dirs don't exist.
STEAM_COMPAT_ROOTS: list[str] = [
    "~/.steam/root/compatibilitytools.d",
    "~/.steam/steam/compatibilitytools.d",
    "~/.local/share/Steam/compatibilitytools.d",
]
STEAM_LIBRARY_ROOTS: list[str] = [
    "~/.steam/root/steamapps/common",
    "~/.steam/steam/steamapps/common",
    "~/.local/share/Steam/steamapps/common",
]
UNIFIDECK_COMPAT_DIR = "~/.local/share/unifideck/compat-tools"
def _compat_tool_roots() -> list[Path]:
    """Every ``compatibilitytools.d`` root to search, in priority order.

    unifideck-managed dir first, then the user Steam compat dirs, then the
    system-wide dirs distro packages install into but Steam never lists
    (CachyOS ``proton-cachyos`` → ``/usr/share/steam/compatibilitytools.d``,
    Arch ``proton-ge-custom``). The pre-0.7.1 resolver scanned only the
    three user dirs, so a system-wide / manifest-registered tool the user
    force-selected was unresolvable and silently fell back to GE-latest.
    """
    roots = [Path(UNIFIDECK_COMPAT_DIR).expanduser()]
    roots += [Path(r).expanduser() for r in STEAM_COMPAT_ROOTS]
    roots += [Path(r) for r in vdf_compat.SYSTEM_COMPAT_DIRS]
    return roots


def _discovered_library_commons() -> list[Path]:
    """``steamapps/common`` for every library named in ``libraryfolders.vdf``.

    Split out from :func:`_steam_library_commons` so it is independently
    stubbable: it touches the real filesystem, which would otherwise leak the
    host's Steam install into any test that points the selector at a tmp dir.
    """
    return [
        lib / "steamapps" / "common" for lib in vdf_compat.steam_library_dirs()
    ]


def _steam_library_commons() -> list[Path]:
    """Every ``steamapps/common`` to search, across ALL Steam libraries.

    The well-known paths in :data:`STEAM_LIBRARY_ROOTS` come first (the main
    install wins), then every additional library Steam records. Steam puts
    Proton in whichever library it chose — routinely an SD card or second
    drive on a Deck — so searching only the well-known paths silently loses a
    perfectly valid Proton the user selected. Deduplicated, order-preserving.
    """
    commons: list[Path] = [Path(r).expanduser() for r in STEAM_LIBRARY_ROOTS]
    commons += _discovered_library_commons()
    seen: set[str] = set()
    unique: list[Path] = []
    for path in commons:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _official_proton_in_library(lib_dir: Path, tool_id: str) -> Path | None:
    """Find ``tool_id``'s ``proton`` script inside one ``steamapps/common``.

    Official Proton tools live under a display-name dir that differs from
    Steam's internal tool id ("proton_experimental" -> "Proton - Experimental",
    "proton_9" -> "Proton 9.0 (Beta)"), and they carry no
    ``compatibilitytool.vdf`` to declare that id, so the compat-tool roots can
    never match them.

    This used to be a hardcoded id->dirname table, which silently rotted: it
    stopped at proton_10, so once Proton 11 shipped, a user selecting it in
    Steam's own Properties > Compatibility got no match here, fell through to
    the remaining tiers, and launched under GE-Proton instead — their choice
    ignored with nothing in the log to say so. Deriving the id from each
    installed dir name instead (see ``vdf_compat.official_proton_alias``)
    covers every past and future Proton release with no table to maintain.
    """
    verbatim = lib_dir / tool_id / "proton"
    if verbatim.is_file():
        return verbatim
    if not lib_dir.is_dir():
        return None
    try:
        entries = sorted(lib_dir.iterdir())
    except OSError:
        return None
    for entry in entries:
        if vdf_compat.official_proton_alias(entry.name) != tool_id:
            continue
        candidate = entry / "proton"
        if candidate.is_file():
            return candidate
    return None


def resolve_proton_path(tool_id: str) -> Path | None:
    """Resolve PROTON path."""
    if not tool_id:
        return None
    # Compat tools (compatibilitytools.d) across user + system-wide roots,
    # manifest-aware: follows a ``compatibilitytool.vdf`` / loose ``.vdf``
    # ``install_path`` (how proton-cachyos is registered) and matches the
    # internal name, display name, or directory name however Steam wrote it.
    resolved = vdf_compat.resolve_compat_tool(tool_id, _compat_tool_roots())
    if resolved is not None:
        return resolved
    for lib_dir in _steam_library_commons():
        official = _official_proton_in_library(lib_dir, tool_id)
        if official is not None:
            return official
    return None
def get_unifideck_proton_tool() -> str | None:
    """Get unifideck PROTON tool."""
    config_path = Path("~/.local/share/unifideck/config.json").expanduser()
    if not config_path.is_file():
        return None
    try:
        import json
        with config_path.open() as f:
            cfg = json.load(f)
        tool = cfg.get("compat", {}).get("proton_tool", "")
        return tool or None
    except (OSError, ValueError):
        return None
def get_saved_proton_tool(store_game_id: str) -> str | None:
    """Return the per-game Proton tool saved by the frontend.

    When the user sets "Force Compatibility" on a Unifideck
    shortcut, the game-details page saves that tool into
    ``proton_settings.json`` (keyed by ``store:game_id``) and
    clears Force Compatibility from Steam so ``RunGame`` runs
    this launcher natively instead of wrapping it in Proton.
    The launcher then applies the saved tool itself — this is
    the lookup that makes the user's choice authoritative.
    """
    if not store_game_id:
        return None
    settings_path = Path(
        "~/.local/share/unifideck/proton_settings.json",
    ).expanduser()
    if not settings_path.is_file():
        return None
    try:
        import json
        with settings_path.open() as f:
            settings = json.load(f)
        entry = settings.get("games", {}).get(store_game_id, "")
        # Pre-0.7.0 wrote each entry as {"proton_tool": "<id>"}; nothing
        # writes that shape anymore, so its presence means this entry
        # hasn't been refreshed since before the rewrite — likely a
        # long-forgotten pin (useLaunchPrep only refreshes it when the
        # game-details page is opened, not on every launch). Extracting
        # and honoring it resurrects a stale, possibly-incompatible
        # Proton choice the user no longer wants (and can't see is even
        # in effect, since Steam's own Force-Compat UI shows whatever
        # was last restored there, not this file). Treat it as no saved
        # override instead and fall through to the normal priority
        # chain — this also fixes the original TypeError crash.
        if isinstance(entry, dict):
            return None
        return entry or None
    except (OSError, ValueError):
        return None
def _read_steam_config_vdf() -> str:
    """Text of the global ``<steam>/config/config.vdf`` (cross-distro), or ""."""
    config_vdf = vdf_compat.find_steam_config_vdf()
    if config_vdf is None:
        return ""
    try:
        return config_vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def get_steam_compat_tool_override(app_id: str) -> str | None:
    """Return the per-app Force-Compat tool Steam recorded for *app_id*.

    ``CompatToolMapping`` lives in the GLOBAL ``config/config.vdf`` — the
    pre-0.7.1 code scanned the per-user ``localconfig.vdf``, which does not
    carry these entries, so this tier generally matched nothing. Root is
    resolved by probing the cross-distro candidates.

    Accepts the AppID in EITHER form and normalises to unsigned.
    ``games.map`` / ``shortcuts.vdf`` store the *signed* 32-bit value
    (``-110954320``) while ``CompatToolMapping`` is keyed by the *unsigned*
    one (``4184012976``), so passing the signed form straight through
    matched nothing and silently dropped the user's choice.
    """
    if not app_id:
        return None
    try:
        appid_int = to_unsigned(app_id)
    except (TypeError, ValueError):
        return None
    content = _read_steam_config_vdf()
    tool = vdf_compat.parse_compat_tool(content, appid_int) or None
    logger.info(
        "[launcher.proton] steam force-compat lookup: appid=%s -> %r",
        appid_int, tool,
    )
    return tool


def get_global_default_tool() -> str | None:
    """Steam's global-default compat tool (``CompatToolMapping["0"]``), or None.

    On Bazzite/CachyOS this is the tool the user expects (e.g.
    ``Proton-CachyOS``); empty on stock SteamOS/Deck.
    """
    return vdf_compat.parse_global_default_compat_tool(_read_steam_config_vdf()) or None
def select_proton_version(
    steam_app_id: str | None = None,
    store_game_id: str | None = None,
) -> tuple[Path, str]:

    """Select PROTON version.

    Priority order:
      1. A live per-app Steam compat override for ``steam_app_id``
         (``config.vdf`` ``CompatToolMapping[appid]``) — the user's
         CURRENT choice, straight from Steam's own
         Properties > Compatibility dialog.
      2. Per-game tool the frontend saved into ``proton_settings.json``
         (the same choice, captured just before the frontend cleared
         Steam's side on the game-details page).
      3. The Unifideck default from ``config.json``.
      4. Steam's GLOBAL default compat tool (``CompatToolMapping["0"]``) —
         the distro/system default (e.g. Proton-CachyOS on CachyOS/Bazzite).
         The launcher runs the shortcut natively, so Steam's own global
         Steam-Play default is never applied unless honored here. Empty on
         stock SteamOS/Deck, so this tier is a no-op there.
      5. The latest GE-Proton released online (downloaded/installed on
         demand), falling back to Proton Experimental when offline.

    **Tiers 1 and 2 were the other way round** and it silently defeated the
    picker. ``proton_settings.json`` is only ever a *shadow* of the live
    value: the frontend writes it at the moment it clears Steam's per-app
    entry, precisely so the choice survives that clear. So whenever a live
    per-app entry DOES exist, it is necessarily at least as fresh as the
    shadow — the user just set it in Steam — while the shadow may be
    arbitrarily old. Preferring the shadow meant a stale entry overrode the
    user's live pick with nothing in the log to explain it. Field-observed
    on Limbo: shadow ``GE-Proton11-3`` beat live ``GE-Proton9-26``, so
    switching Proton in Steam's dialog appeared to do nothing at all no
    matter which build was chosen. Reading the live value first makes the
    picker authoritative and removes any dependence on frontend timing.
    """
    tried: list[str] = []
    steam_tool = (
        get_steam_compat_tool_override(steam_app_id) if steam_app_id else None
    )
    if steam_tool:
        path = _resolve_logged("steam", steam_tool, tried)
        if path:
            return path, steam_tool
    saved_tool = get_saved_proton_tool(store_game_id) if store_game_id else None
    if saved_tool:
        path = _resolve_logged("saved", saved_tool, tried)
        if path:
            return path, saved_tool
    unifideck_tool = get_unifideck_proton_tool()
    if unifideck_tool:
        path = _resolve_logged("unifideck", unifideck_tool, tried)
        if path:
            return path, unifideck_tool
    global_tool = get_global_default_tool()
    if global_tool:
        path = _resolve_logged("global-default", global_tool, tried)
        if path:
            return path, global_tool
    return _default_latest_ge(tried)


def select_managed_ge_proton() -> tuple[Path, str]:
    """The plugin-managed latest GE-Proton (the tier-5 default in isolation).

    Bypasses the saved / per-app / distro-default tiers to return the
    known-good GE-Proton the plugin installs and validates. Used as the
    recovery Proton when the normally-selected one hangs at runtime —
    e.g. a broken auto-updated Proton-Experimental that wedged install
    warmup (see ``prefix_warmup``). Falls back to Proton Experimental
    only if GE is genuinely unavailable (offline + none installed), same
    as the default tier.
    """
    return _default_latest_ge([])


def _resolve_logged(source: str, tool: str, tried: list[str]) -> Path | None:
    """Record the attempt, resolve + validate the tool, log on success.

    Returns ``None`` (skip this tier, fall through to the next) when the
    tool resolves to a *structurally incomplete* Proton — a truncated
    or half-extracted install whose ``umu-run`` operations would hang.
    This degrades a broken saved / per-app / distro-default Proton
    gracefully into the managed-GE default instead of handing umu a
    Proton that wedges prefix setup. (A build that is structurally
    complete but hangs at *runtime* is caught later by the compat-step
    timeout — see ``run_umu_with_retry(timeout=...)`` and the warmup
    GE-retry.)
    """
    tried.append(f"{source}:{tool}")
    path = resolve_proton_path(tool)
    if not path:
        return None
    if not ge_installer.is_proton_install_complete(path):
        logger.warning(
            "[launcher.proton] %s tool %s resolved to an incomplete "
            "install (%s) — skipping, will fall back",
            source, tool, path.parent,
        )
        return None
    logger.info("[launcher.proton] selected via %s tool: %s", source, tool)
    return path


class _GeDownloadAnnouncer:
    """A ``progress_cb`` that toasts once when a real download starts.

    ``ge_installer`` invokes this per byte chunk, but only when an
    actual download happens (it returns early when GE is already
    installed). We fire a single "downloading Proton" toast on the
    first chunk and record ``fired`` so the caller knows whether to
    also toast "ready". Best-effort — a toast failure never breaks
    Proton selection.
    """

    def __init__(self) -> None:
        self.fired = False

    def __call__(self, _done: int, _total: int) -> None:
        if self.fired:
            return
        self.fired = True
        try:
            from unifideck.launcher.frontend_bridge import launcher_toast
            launcher_toast(
                "toasts.launcher.downloadingProton",
                i18n_title_key="toasts.launcher.installingProton",
            )
        except Exception:
            logger.debug("[launcher.proton] GE download toast failed", exc_info=True)


def _announce_ge_ready(tag: str) -> None:
    """Toast that the just-downloaded GE-Proton is ready (best-effort)."""
    try:
        from unifideck.launcher.frontend_bridge import launcher_toast
        launcher_toast(
            "toasts.launcher.protonReadyBody",
            i18n_title_key="toasts.launcher.protonReadyTitle",
            i18n_params={"version": tag},
        )
    except Exception:
        logger.debug("[launcher.proton] GE ready toast failed", exc_info=True)


def _default_latest_ge(tried: list[str]) -> tuple[Path, str]:
    """Default tier: external GE-Proton, latest GE-Proton online, else Experimental.

    1. External manager — if an externally managed tool (e.g. ProtonPlus
       'Proton-GE Latest') exists, is complete, and is not older than our
       cached GE build, use it as default.
    2. Fast path — if the background installer recorded a latest tag
       (``proton_ge_latest.json``) and it is validly installed, use it
       without touching the network.
    3. Safety net — fetch the newest GE-Proton tag and download/install
       it on demand (bounded; offline returns ``None`` quickly).
    4. Fallback — Proton Experimental (the only fallback by design;
       older local GE versions stay user-selectable via Force Compat).
    """
    external = external_ge.find_external_ge_proton()
    cached = ge_installer.read_cached_latest_tag()
    cached_path = ge_installer.installed_ge_proton_path(cached) if cached else None

    if external:
        ext_path, ext_id, ext_ver = external
        if cached and cached_path and ext_ver and external_ge.is_ge_outdated(ext_ver, cached):
            logger.info(
                "[launcher.proton] external GE (%s) is older than cached Unifideck GE (%s); "
                "preferring Unifideck GE",
                ext_ver, cached,
            )
            tried.append(f"latest-ge-cached:{cached}")
            return cached_path, cached
        tried.append(f"external-ge:{ext_id}")
        logger.info(
            "[launcher.proton] selected externally managed GE-Proton: %s (%s)",
            ext_id, ext_ver or "unknown",
        )
        return ext_path, ext_id

    if cached and cached_path:
        tried.append(f"latest-ge-cached:{cached}")
        logger.info(
            "[launcher.proton] selected cached latest GE-Proton: %s", cached,
        )
        return cached_path, cached

    # On-demand download at launch time — the background installer
    # hasn't finished (or never ran). This is otherwise silent, leaving
    # the user staring at a frozen-looking launch while a ~hundreds-of-MB
    # Proton downloads, so toast when a real download starts/finishes.
    # ``progress_cb`` fires only during the actual byte stream, so the
    # toast never appears when GE is already installed (no download).
    announcer = _GeDownloadAnnouncer()
    result = ge_installer.ensure_latest_ge(progress_cb=announcer)
    if result:
        path, tag = result
        tried.append(f"latest-ge:{tag}")
        logger.info("[launcher.proton] selected latest GE-Proton: %s", tag)
        if announcer.fired:  # only when a download actually happened
            _announce_ge_ready(tag)
        return path, tag

    tried.append("fallback:proton_experimental")
    experimental = resolve_proton_path("proton_experimental")
    if experimental:
        logger.info(
            "[launcher.proton] GE-Proton unavailable; "
            "falling back to Proton Experimental",
        )
        return experimental, "proton_experimental"

    raise ProtonUnavailableError(
        "No usable Proton compat tool found",
        context={"tried": tried},
    )
