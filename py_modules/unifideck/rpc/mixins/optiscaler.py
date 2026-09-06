"""OptiScalerRPCMixin — patch/unpatch Frame Generation (OptiScaler) per game.

Powers the "Frame Generation (OptiScaler)…" item injected into the native
game context menu. Replaces the broken
workflow of pasting Decky-Framegen's ``~/fgmod/fgmod %command%`` into a
Unifideck shortcut's Steam Launch Options: Unifideck shortcuts always point
``Exe`` at ``bin/unifideck-launcher`` with ``"<store>:<game_id>"`` in
``LaunchOptions`` (see ``services.shortcut.reconcile_phases._build_shortcut_entry``),
never the game's own path — so ``fgmod.sh``'s ``*.exe`` argv-sniffing (see its
source, ``fgmod.sh``) never finds a match, and its ``STEAM_COMPAT_INSTALL_PATH``
fallback isn't populated at that point either (Steam launches the launcher
script directly, not through Proton, so that var is never set for it). Users
reported DLSS/FSR files landing next to the launcher script instead of in the
game folder, or the game simply not being found at all by the plugin's
auto-detection.

Unifideck already knows the correct install directory (games.map ``work_dir``,
the same helper ``ExecutableRPCMixin`` uses) — so this mixin calls the SAME
``~/fgmod/fgmod`` wrapper Decky-Framegen installs, but passes that directory
directly as its one argument (the "standalone" invocation path in
``fgmod.sh`` — ``if [[ $# -eq 1 ]] ... exe_folder_path=$1``), skipping the
broken argv-sniffing entirely. Decky-Framegen itself remains required — this
mixin never downloads or bundles OptiScaler/DLSS-Enabler; it only detects
that ``~/fgmod/fgmod`` exists and drives it with the right path.

The patch/unpatch/status target directory is the ``.exe``'s OWN folder
(``_patch_target_dir``), not necessarily games.map's ``work_dir`` install
root — some installs extract into a nested subfolder repeating the title
(``Ghost.of.Tsushima/Ghost.of.Tsushima/…``), and Windows only searches the
launched EXE's own directory for DLLs, not the install root. Patching at
``work_dir`` in that case silently drops the DLSS-Enabler DLLs one level too
high, where the game process never finds them, even though fgmod itself
reports success. ``_patch_target_dir`` prefers the games.map row's ``exe``
column (same ground truth ``ExecutableRPCMixin`` uses) and falls back to
``work_dir`` only when no resolvable exe exists yet.

Both subprocess calls (``fgmod`` and the uninstaller) run ``bash`` scripts
inside the Decky plugin's own PyInstaller-frozen process, whose
``os.environ`` carries a poisoned ``LD_LIBRARY_PATH``/``LD_PRELOAD`` (see
``sanitize_frozen_loader_env`` — the same pollution that broke every
GOG/Amazon/Ubisoft launch before it was fixed there). Left untouched, that
``LD_LIBRARY_PATH`` shadows the system's own ``libreadline``, and ``bash``
itself fails to start: ``bash: symbol lookup error: bash: undefined symbol:
rl_trim_arg_from_keyseq`` (rc=127) — observed in the field on every patch
attempt. Both subprocess environments are sanitized before use for this
reason.

fgmod version drift: Decky-Framegen's ``fgmod``/``fgmod-uninstaller.sh`` have
been rewritten upstream since this mixin was first written against the
public ``fgmod.sh`` source. The OLD version wrote a single fixed marker
(``dlss-enabler.dll``) and dropped a per-game ``fgmod-uninstaller.sh`` copy
into the patched folder. CURRENT installs instead write a whole different
set of files (``OptiScaler.ini``, ``fakenvapi.*``, ``D3D12_Optiscaler/``,
...) and ship exactly ONE shared uninstaller script in ``~/fgmod/`` — no
per-game copy at all. A status/uninstall check hardcoded to the old
single-marker convention silently went stale: fgmod itself logged a
successful patch, but this mixin kept reporting "not patched" and offering
"Patch" instead of "Remove patch". ``_is_patched``/``_resolve_uninstaller``
now check every fingerprint either generation is known to leave, so both
old and current fgmod installs are recognised correctly.

One env-var config surface, not two: earlier revisions of this mixin had
their OWN ``games.<store>:<game_id>.optiscaler_env`` config key + a
dedicated env editor inside this modal, separate from the general
"Environment variables…" item (``GameEnvRPCMixin``). Two places to set env
vars for the same game was confusing (which one applies where?) and
OptiScaler's own env-var convention (``Section_Option=value``, see
Decky-Framegen's README) is just regular environment variables — no
different from ``MANGOHUD=1``. So ``apply_optiscaler_patch`` now reads the
SAME general overrides ``GameEnvRPCMixin`` stores and merges them into the
patch subprocess too — set them once in "Environment variables…", and they
apply to both the game's own launch AND the OptiScaler patch step.

Three RPCs:

* ``get_optiscaler_status`` — whether ``~/fgmod/fgmod`` is installed, and
  whether this game's install dir already has ANY known patch fingerprint
  (see ``_PATCH_FINGERPRINT_NAMES`` — i.e. is patched).
* ``apply_optiscaler_patch`` — run ``fgmod <install_dir>`` with this game's
  general environment-variable overrides (``GameEnvRPCMixin``) merged in.
* ``remove_optiscaler_patch`` — run the resolved uninstaller (per-game copy
  on older fgmod, the shared central script on current fgmod — see
  ``_resolve_uninstaller``).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from unifideck.launcher.proton.infrastructure.core import sanitize_frozen_loader_env
from unifideck.rpc import RpcError
from unifideck.rpc.mixins.game_env import _load_env as _load_general_env_overrides

logger = logging.getLogger(__name__)

# Where Decky-Framegen's ``prepare.sh`` installs the wrapper (see fgmod's own
# README: "This will create a folder called fgmod in your home directory").
_FGMOD_DIR = Path("~/fgmod").expanduser()
_FGMOD_SCRIPT = _FGMOD_DIR / "fgmod"

# The central uninstaller script fgmod ships in its OWN directory (NOT
# dropped per-game — see the module docstring's "fgmod version drift" note).
_UNINSTALLER_SCRIPT = _FGMOD_DIR / "fgmod-uninstaller.sh"

# Some OLDER fgmod builds (the version documented by fgmod.sh's public
# GitHub source at the time this mixin was first written) DID drop a
# per-game copy instead of shipping one central script. Still honoured as
# the first choice in ``_resolve_uninstaller`` for users on that vintage.
_LOCAL_UNINSTALLER_NAME = "fgmod-uninstaller.sh"

# Files/dirs ANY observed fgmod build leaves behind in the patched game
# folder — used as "is this game patched" fingerprints. fgmod itself moved
# from a single fixed marker (``dlss-enabler.dll``, old builds) to its own
# multi-file ``has_patch_fingerprint()`` check (current builds, matched
# here) as OptiScaler's own file layout evolved; checking only one specific
# name silently went stale and "Patch" kept showing after a successful
# patch. Any ONE of these existing is sufficient evidence.
_PATCH_FINGERPRINT_NAMES = (
    "OptiScaler.ini",
    "fakenvapi.dll",
    "fakenvapi.ini",
    "dlssg_to_fsr3_amd_is_better.dll",
    "D3D12_Optiscaler",  # directory
    "FRAMEGEN_PATCH",  # forward-compat sentinel some fgmod builds drop
    # Legacy (pre-refactor) fgmod builds:
    "dlss-enabler.dll",
    _LOCAL_UNINSTALLER_NAME,
)

# Generous but bounded — fgmod copies a handful of small DLLs; it should
# never legitimately run long. Guards against a wedged/interactive prompt
# (fgmod can zenity-popup on error) hanging the RPC forever.
_PATCH_TIMEOUT_SECONDS = 30.0


def _is_fgmod_installed() -> bool:
    return _FGMOD_SCRIPT.is_file()


def _is_patched(install_dir: str) -> bool:
    """Whether ``install_dir`` already has a fgmod patch applied.

    Checks every known fingerprint fgmod has EVER left behind (see
    ``_PATCH_FINGERPRINT_NAMES``) — matching fgmod's own current
    ``has_patch_fingerprint()`` bash function plus the older single-marker
    convention, rather than any ONE fixed name. fgmod's own file layout has
    changed between versions (old builds wrote ``dlss-enabler.dll`` +
    a per-game ``fgmod-uninstaller.sh``; current builds write
    ``OptiScaler.ini``/``fakenvapi.*``/``D3D12_Optiscaler``/etc and ship a
    single CENTRAL uninstaller instead), and checking a single stale name
    is exactly what made a freshly, successfully patched game keep showing
    "Patch" instead of "Remove patch" in the field even though fgmod itself
    logged success.
    """
    if not install_dir:
        return False
    return any(
        os.path.exists(os.path.join(install_dir, name))
        for name in _PATCH_FINGERPRINT_NAMES
    )


def _resolve_uninstaller(install_dir: str) -> str | None:
    """Path to the uninstaller script to run for ``install_dir``.

    Newer fgmod builds ship ONE uninstaller in ``~/fgmod/`` that takes the
    game's exe/folder as an argument (mirrors ``fgmod``'s own invocation
    convention) instead of dropping a per-game copy. Older builds still
    drop a local copy INTO the game folder. Prefer the per-game copy when
    present (keeps working for anyone still on that fgmod vintage); fall
    back to the shared central script otherwise.
    """
    local = os.path.join(install_dir, _LOCAL_UNINSTALLER_NAME)
    if os.path.isfile(local):
        return local
    if _UNINSTALLER_SCRIPT.is_file():
        return str(_UNINSTALLER_SCRIPT)
    return None


async def _run_fgmod_subprocess(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
) -> tuple[int | None, str]:
    """Run an fgmod (un)patch command; return ``(returncode, output)``.

    Kills the process on timeout. Shared by
    ``apply_optiscaler_patch``/``remove_optiscaler_patch`` so neither owns a
    second copy of the create/wait/timeout dance. ``FileNotFoundError``
    (missing script) propagates to the caller, which maps it to its own
    ``fgmod_not_installed`` / ``not_patched`` RpcError.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_PATCH_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    output = (stdout or b"").decode(errors="replace").strip()
    return proc.returncode, output


class OptiScalerRPCMixin:
    """Frame Generation (OptiScaler) patch/unpatch RPC surface."""

    config: Any
    services: Any

    def _install_dir(self, store: str, game_id: str) -> str:
        """Read-only install ROOT from games.map ``work_dir`` (never written).

        Same resolution `ExecutableRPCMixin` uses. NOT necessarily where the
        game's own ``.exe`` lives — see ``_patch_target_dir``, which is what
        patch/unpatch/status actually use. Kept as the fallback for when no
        games.map row (and thus no resolvable exe) exists yet.
        """
        from unifideck.services.cloud_save.save_location_resolver import (
            _install_path_from_games_map,
        )
        return str(_install_path_from_games_map(store, game_id, self.config))

    async def _patch_target_exe(self, store: str, game_id: str) -> str | None:
        """The resolved ``.exe`` absolute path, if games.map has one.

        Needed (rather than just the folder) for the central uninstaller
        script, which — unlike ``fgmod`` itself — has NO "single directory
        argument" standalone mode: it only recognises ``$1`` when it ends
        in ``.exe``, else falls back to ``STEAM_COMPAT_INSTALL_PATH`` (see
        ``_resolve_uninstaller``'s docstring). Returns ``None`` when no
        games.map row (or no exe file on disk) exists — callers fall back
        to ``STEAM_COMPAT_INSTALL_PATH`` instead.
        """
        shortcut = getattr(getattr(self, "services", None), "shortcut", None)
        if shortcut is None:
            return None
        try:
            entry = await shortcut.get_entry_for_game_key(store, game_id)
        except Exception:  # pragma: no cover - best-effort
            return None
        if entry and entry.exe and await asyncio.to_thread(os.path.isfile, entry.exe):
            return str(entry.exe)
        return None

    async def _patch_target_dir(self, store: str, game_id: str) -> str:
        """Directory to hand to fgmod — the ``.exe``'s OWN folder, not the
        install root.

        Many installs extract into a nested subfolder repeating the title
        (e.g. ``Ghost.of.Tsushima/Ghost.of.Tsushima/…``), so the real
        ``.exe`` lives one or more levels below games.map's ``work_dir``.
        Patching at ``work_dir`` drops the DLSS-Enabler DLLs next to the
        OUTER folder, where the game process never finds them — Windows'
        DLL search order starts at the launched EXE's own directory, not
        its install root. Reported in the field: fgmod reports success, but
        the game still runs unpatched because the files landed one level too
        high.

        Prefer the games.map row's own ``exe`` column (the launcher's actual
        launch target for this game — the same ground truth
        ``ExecutableRPCMixin._current_rel`` reads) and use ITS directory.
        Falls back to the plain install root only when no games.map row (or
        no exe file at that path) exists yet — e.g. before first launch.
        """
        shortcut = getattr(getattr(self, "services", None), "shortcut", None)
        if shortcut is not None:
            try:
                entry = await shortcut.get_entry_for_game_key(store, game_id)
            except Exception:  # pragma: no cover - best-effort
                entry = None
            if entry and entry.exe and await asyncio.to_thread(os.path.isfile, entry.exe):
                return os.path.dirname(str(entry.exe))
        return self._install_dir(store, game_id)

    async def get_optiscaler_status(self, store: str, game_id: str) -> Any:
        """Report fgmod availability, this game's patch state, and which
        env vars a patch would apply.

        ``env`` is READ-ONLY here — it mirrors the general "Environment
        variables…" store (``GameEnvRPCMixin``) so the modal can show what
        WOULD be passed to ``fgmod`` on the next ``apply_optiscaler_patch``,
        without this mixin owning a second copy of that config. Editing
        happens in the general modal, not here.
        """
        if not store or not game_id:
            raise RpcError("invalid_args", store=store, game_id=game_id)
        install_dir = await self._patch_target_dir(store, game_id)
        fgmod_installed = _is_fgmod_installed()
        patched = await asyncio.to_thread(_is_patched, install_dir)
        return {
            "fgmod_installed": fgmod_installed,
            "install_dir": install_dir,
            "patched": patched,
            "env": _load_general_env_overrides(self.config, store, game_id),
        }

    async def apply_optiscaler_patch(self, store: str, game_id: str) -> Any:
        """Run ``fgmod <install_dir>`` with this game's env overrides merged in.

        Calls Decky-Framegen's own installed wrapper directly with the
        correct install directory as its sole argument — the "standalone"
        invocation ``fgmod.sh`` supports — instead of relying on its normal
        ``%command%`` argv-sniffing, which never finds a real ``.exe`` for a
        Unifideck shortcut (``Exe`` always points at ``unifideck-launcher``).

        Env overrides come from the GENERAL "Environment variables…" store
        (``GameEnvRPCMixin`` — set once, applies everywhere for this game),
        not a separate OptiScaler-only config.
        """
        if not store or not game_id:
            raise RpcError("invalid_args", store=store, game_id=game_id)
        if not _is_fgmod_installed():
            raise RpcError("fgmod_not_installed", store=store, game_id=game_id)
        install_dir = await self._patch_target_dir(store, game_id)
        if not install_dir or not await asyncio.to_thread(os.path.isdir, install_dir):
            raise RpcError("install_dir_unresolved", store=store, game_id=game_id)

        general_env = _load_general_env_overrides(self.config, store, game_id)
        env = dict(os.environ)
        sanitize_frozen_loader_env(env)
        env.update(general_env)

        try:
            returncode, output = await _run_fgmod_subprocess(
                [str(_FGMOD_SCRIPT), install_dir], env, cwd=None,
            )
        except TimeoutError:
            raise RpcError(
                "patch_timed_out", store=store, game_id=game_id,
            ) from None
        except FileNotFoundError as e:
            raise RpcError("fgmod_not_installed", store=store, game_id=game_id) from e

        if returncode != 0:
            logger.warning(
                "[OptiScaler] patch failed for %s:%s (rc=%s): %s",
                store, game_id, returncode, output,
            )
            raise RpcError(
                "patch_failed", store=store, game_id=game_id, output=output,
            )
        logger.info(
            "[OptiScaler] patched %s:%s (install_dir=%s, env_overrides=%s)",
            store, game_id, install_dir, sorted(general_env),
        )
        return {"success": True, "output": output, "env": general_env}

    async def remove_optiscaler_patch(self, store: str, game_id: str) -> Any:
        """Run the ``fgmod-uninstaller.sh`` that patching dropped into the game dir."""
        if not store or not game_id:
            raise RpcError("invalid_args", store=store, game_id=game_id)
        install_dir = await self._patch_target_dir(store, game_id)
        if not install_dir or not await asyncio.to_thread(os.path.isdir, install_dir):
            raise RpcError("install_dir_unresolved", store=store, game_id=game_id)
        if not _is_patched(install_dir):
            raise RpcError("not_patched", store=store, game_id=game_id)
        uninstaller = _resolve_uninstaller(install_dir)
        if not uninstaller:
            raise RpcError("not_patched", store=store, game_id=game_id)
        # A per-game copy (older fgmod builds) needs no argument — it
        # already runs FROM inside that folder via ``cwd``. The central
        # script (current builds) has no "just a directory" mode: it only
        # recognises an arg ending in ``.exe``, so pass the resolved exe
        # when we have one and lean on STEAM_COMPAT_INSTALL_PATH (below)
        # as the fallback it itself checks when we don't.
        is_central = uninstaller == str(_UNINSTALLER_SCRIPT)
        exe = await self._patch_target_exe(store, game_id) if is_central else None
        argv = ["bash", uninstaller, exe] if is_central and exe else ["bash", uninstaller]

        env = dict(os.environ)
        sanitize_frozen_loader_env(env)
        if is_central:
            env["STEAM_COMPAT_INSTALL_PATH"] = install_dir

        try:
            returncode, output = await _run_fgmod_subprocess(
                argv, env, cwd=install_dir,
            )
        except TimeoutError:
            raise RpcError(
                "unpatch_timed_out", store=store, game_id=game_id,
            ) from None
        except FileNotFoundError as e:
            raise RpcError("not_patched", store=store, game_id=game_id) from e

        if returncode != 0:
            logger.warning(
                "[OptiScaler] unpatch failed for %s:%s (rc=%s): %s",
                store, game_id, returncode, output,
            )
            raise RpcError(
                "unpatch_failed", store=store, game_id=game_id, output=output,
            )
        logger.info("[OptiScaler] unpatched %s:%s", store, game_id)
        return {"success": True, "output": output}
