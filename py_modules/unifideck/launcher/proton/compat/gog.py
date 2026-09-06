"""compat/gog.py — GOG-specific launch helpers (Comet + launcher fallback).

* :func:`start_comet` — launch the bundled ``comet`` (a GOG Galaxy SDK
  reimplementation) in the background so the game gets online features
  (achievements, multiplayer). Tokens come from the GOG token file.
* :func:`resolve_fallback_exe` — when a GOG game exits suspiciously fast,
  detect that the primary ``goggame-*.info`` playTask is a launcher/tool
  stub and return the real game-category exe to retry with.

Standalone: no ``unifideck.stores`` imports (launcher's slim Python).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.launcher.proton.infrastructure.umu_runtime import (
    run_umu_with_retry,
)

from .gog_setup.common import AUTH_CONFIG

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)

# gogdl's auth file is plain JSON, keyed by GOG OAuth client_id →
# {access_token, refresh_token, user_id, ...}. It uses the GOG Galaxy client —
# the same tokens Comet needs — and unlike the plugin's encrypted
# ``gog_token.json`` it's readable from the slim launcher process
# (which can't load the cryptography chain to decrypt the other one).
# MUST match the path the plugin actually writes / gogdl save-sync reads
# (``GOGConfig.auth_config_path``): a FLAT ``gogdl_auth.json``, NOT a
# ``gogdl/auth.json`` subdir. The old subdir path never existed, so
# ``_read_gog_tokens`` always returned None and Comet was silently skipped
# ("no GOG tokens — Comet online features off") — GOG games ran offline
# with no achievement capture. The cloud-save save-sync refreshes this file
# before launch, so it's fresh by the time Comet starts.
#
# Imported from ``gog_setup.common`` rather than re-declared: the redist
# downloader there had drifted back to the dead subdir path and silently
# skipped every GOG redistributable install. One definition, no third copy.
# ``common`` is stdlib-only at import time, so this stays launcher-safe.
_GOGDL_AUTH_FILE = AUTH_CONFIG
# A launched exe that exits faster than this is treated as a possible
# broken launcher stub (real launchers/games run far longer).
EARLY_EXIT_SECONDS = 15
# Seconds Comet keeps running after the game (its only SDK client)
# disconnects. Comet's quit flag makes it upload any pending achievement
# unlocks to GOG and then self-exit; COMET_IDLE_WAIT controls that delay.
# Kept short so the post-play achievement reconcile isn't held up.
COMET_IDLE_WAIT_SECONDS = 2
# Upper bound we wait for that flush + self-exit before forcing Comet down.
# MUST exceed COMET_IDLE_WAIT plus the upload round-trip, or we'd kill Comet
# mid-upload and lose the session's just-earned achievements.
COMET_FLUSH_SECONDS = 10


def _read_gog_tokens() -> tuple[str, str, str] | None:
    """Return (access, refresh, user_id) from gogdl's plain auth, or None."""
    if not _GOGDL_AUTH_FILE.is_file():
        return None
    try:
        data = json.loads(_GOGDL_AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        access = entry.get("access_token") or ""
        refresh = entry.get("refresh_token") or ""
        if access and refresh:
            return access, refresh, str(entry.get("user_id") or "")
    return None


def start_comet(plan: ProtonLaunchPlan) -> subprocess.Popen[bytes] | None:
    """Start Comet (GOG Galaxy SDK) in the background, or None.

    Best-effort: missing binary/tokens just means no online features.
    """
    comet = plan.context.plugin_dir / "bin" / "comet"
    if not comet.is_file():
        return None
    tokens = _read_gog_tokens()
    if tokens is None:
        logger.info("[compat.gog] no GOG tokens — Comet online features off")
        return None
    access, refresh, user_id = tokens
    args = [
        str(comet),
        "--username", "GOGUser",
        "--access-token", access,
        "--refresh-token", refresh,
        "--quit",
    ]
    if user_id:
        args += ["--user-id", user_id]
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            # COMET_IDLE_WAIT: with the quit flag set, this is how long Comet
            # lingers after the game disconnects before flushing unlocks and
            # exiting. COMET_LOG raises its log level for diagnosability (goes
            # to the inherited stderr; set comet output to a file to capture).
            env={
                **os.environ,
                "COMET_IDLE_WAIT": str(COMET_IDLE_WAIT_SECONDS),
                "COMET_LOG": os.environ.get("COMET_LOG", "info"),
            },
        )
        logger.info("[compat.gog] Comet started (pid=%s)", proc.pid)
        return proc
    except OSError as e:
        logger.warning("[compat.gog] Comet failed to start: %s", e)
        return None


def _shutdown_comet(comet: subprocess.Popen[bytes]) -> None:
    """Let Comet flush pending unlocks and self-exit, forcing it down only if it stalls.

    Comet (started with the quit flag) uploads any achievements the game
    earned this session to GOG's servers and exits shortly after the game —
    its only SDK client — disconnects. We MUST give it that window: the old
    code ``terminate()``d it immediately on game-exit, which could kill it
    mid-upload and lose the session's unlocks (and any post-play read-back
    would then miss them). Only force it down if it overruns the bound.

    Synchronous (``Popen.wait`` blocks) — call via ``asyncio.to_thread``.
    """
    try:
        comet.wait(timeout=COMET_FLUSH_SECONDS)
        logger.info(
            "[compat.gog] Comet flushed unlocks and exited (rc=%s)",
            comet.returncode,
        )
        return
    except subprocess.TimeoutExpired:
        logger.warning(
            "[compat.gog] Comet didn't exit within %ss — terminating",
            COMET_FLUSH_SECONDS,
        )
    with contextlib.suppress(Exception):
        comet.terminate()
    try:
        comet.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            comet.kill()


def _load_play_tasks(info_file: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(info_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    tasks = data.get("playTasks", []) if isinstance(data, dict) else []
    return [t for t in tasks if isinstance(t, dict)]


def resolve_fallback_exe(install_path: str) -> str | None:
    """Return the real game exe when the primary playTask is a stub.

    Only fires when the primary ``goggame-*.info`` playTask is a
    ``launcher``/``tool`` category — real game-category primaries are
    never bypassed (mirrors staging). Returns the first game-category
    ``FileTask`` exe that exists on disk, or None.
    """
    for info_file in Path(install_path).glob("goggame-*.info"):
        tasks = _load_play_tasks(str(info_file))
        primary = next((t for t in tasks if t.get("isPrimary")), None)
        if not primary or str(
            primary.get("category", "")
        ).lower() not in ("launcher", "tool"):
            return None
        for t in tasks:
            if (
                t.get("category") == "game"
                and t.get("type") == "FileTask"
                and t.get("path")
            ):
                candidate = Path(install_path) / str(t["path"]).replace("\\", "/")
                if candidate.is_file():
                    return str(candidate)
        return None
    return None


def _read_required_launch_args(work_dir: Path, exe_path: Path) -> list[str]:
    """Return the ``goggame-*.info`` playTask arguments required to launch ``exe_path``.

    GOG's Windows DOSBox/ScummVM packages mark the *generic* wrapper exe
    (``DOSBOX\\dosbox.exe``, ``SCUMMVM\\scummvm.exe``) as the primary
    playTask, with the actual per-game ``-conf``/``-c`` flags living in
    that task's own ``arguments`` string — e.g. ``-conf "..\\game.conf"
    -conf "..\\game_single.conf" -noconsole -c "exit"``. Without them the
    wrapper launches with no game config at all (GitHub #248: "loading
    the generic DOSBOX .exe instead of the .bat"). Mirrors
    ``_read_amazon_fuel_args`` in ``handlers/generic.py`` — re-derived
    fresh from a file already on disk every launch, no persistence
    through ``games.map``. Matched by resolved-path equality using
    ``GOGExeResolver._resolve_case_insensitive`` — GOG's manifests are
    authored on Windows' case-insensitive filesystem and can say
    ``DOSBOX\\dosbox.exe`` while the real extracted file is
    ``DOSBOX/DOSBox.exe`` (confirmed against real "Betrayal at Krondor"
    / "Caesar II" packages); a naive case-sensitive join here never
    matches ``exe_path`` (already resolved through that same
    case-correction upstream, in ``GOGExeResolver``) and silently drops
    every required arg. So the fallback exe (a different playTask) gets
    its own arguments, not the primary's. Returns ``[]`` on any failure
    or no match — the exe still launches, just without extra args, same
    as Amazon's.
    """
    from unifideck.stores.gog.exe_resolver import resolve_case_insensitive

    exe_resolved = exe_path.resolve()
    for info_file in work_dir.glob("goggame-*.info"):
        for task in _load_play_tasks(str(info_file)):
            task_path = task.get("path")
            if not task_path:
                continue
            candidate = Path(
                resolve_case_insensitive(work_dir, str(task_path)),
            ).resolve()
            if candidate != exe_resolved:
                continue
            args = task.get("arguments")
            if not args:
                return []
            try:
                return shlex.split(str(args))
            except ValueError:
                logger.warning(
                    "[compat.gog] unparsable playTask arguments for %s: %r",
                    exe_path, args,
                )
                return []
    return []


def _install_language(work_dir: Path) -> str:
    """Read the user's install-time language from the ``.unifideck-id``
    marker, VERBATIM.

    Returns the exact language code the user picked (whatever format
    gogdl reported it in — ``"esp"``, ``"Spanish"``, ``"es-ES"``…).
    The value is passed through unchanged; downstream consumers only
    *normalize for matching*, never substitute the code. Returns ``""``
    when no language was recorded, so callers fall back appropriately.
    """
    marker = work_dir / ".unifideck-id"
    if marker.is_file():
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(marker.read_text(encoding="utf-8"))
            return str(data.get("language") or "")
    return ""


async def _run_umu_exe(
    plan: ProtonLaunchPlan, exe_path: Path, work_dir: Path, *, max_attempts: int = 2,
) -> int:
    """Run a Windows exe through umu (shared by primary + fallback).

    ``max_attempts`` is the umu-level retry count for THIS exe. The
    primary exe keeps the default (2); the launcher-stub fallback in
    :func:`_run_gog_with_fallback` passes 1, because that fallback is
    itself a higher-level retry (a *different* exe) — running a full
    2-attempt umu retry on it too made one Play press fire the retry
    toast (and, for corruption codes, wipe the shared runtime cache) up
    to 4× (2 primary + 2 fallback). One attempt on the fallback caps it.

    ``work_dir`` (the install root, where ``goggame-*.info`` lives — NOT
    necessarily ``exe_path``'s own parent, e.g. a DOSBox wrapper under
    ``DOSBOX\\dosbox.exe``) is searched for this exe's playTask
    ``arguments`` (see :func:`_read_required_launch_args`); those are
    prepended before the user's own launch options, mirroring
    ``_read_amazon_fuel_args`` in ``handlers/generic.py``.
    """
    cwd = exe_path.parent if exe_path.parent.is_dir() else None
    required_args = _read_required_launch_args(work_dir, exe_path)
    argv: list[str] = []
    argv.extend([str(plan.python_bin), str(plan.umu_wrapper), str(exe_path)])
    argv.extend(required_args)
    argv.extend(plan.state.game_args)
    return await run_umu_with_retry(
        argv, env=plan.env, cwd=cwd, on_start=plan.on_process_start,
        max_attempts=max_attempts,
    )


async def run_gog_launch(plan: ProtonLaunchPlan) -> int:
    """Full GOG Windows launch: setup → Comet → run (with stub fallback).

    Returns the umu exit code; ``generic_launch`` maps it to a Result.
    GOG *native* (start.sh) never reaches here — it goes via launch_native.
    """
    work_dir = Path(plan.context.work_dir or plan.context.exe_path.parent)
    await _apply_gog_prelaunch_setup(plan, work_dir)
    plan.env["PROTON_ENABLE_NVAPI"] = "1"
    return await _run_gog_with_fallback(plan, work_dir)


async def _apply_gog_prelaunch_setup(
    plan: ProtonLaunchPlan, work_dir: Path,
) -> None:
    """Best-effort first-launch setup: language, Galaxy stub, redistributables.

    Each step is independent and never blocks the launch on failure.
    """
    # NOTE: We deliberately do NOT touch ``goggame-*.info``. gogdl
    # already writes the correct per-language ``goggame-*.info`` for
    # whatever ``--lang`` we pass at install (verified: ``--lang
    # es-MX`` → ``language="Latin American Spanish"``). The game reads
    # that field at runtime to pick its language. Rewriting it here —
    # as the removed ``apply_gog_language`` did — corrupted GOG's own
    # value (e.g. clobbering "Latin American Spanish" with the raw code
    # "es-MX"), so the game fell back to English. The install-time file
    # is authoritative; leave it alone.

    # GalaxyCommunication.exe stub (offline SDK).
    try:
        from unifideck.launcher.proton.fixes.galaxy_stub import (
            install_galaxy_stub,
        )
        install_galaxy_stub(
            str(plan.prefix_path), plugin_dir=plan.context.plugin_dir,
        )
    except Exception:
        logger.warning("[compat.gog] galaxy stub failed", exc_info=True)

    # GOG redistributables + setup scripts (first launch, marker-guarded).
    try:
        from .gog_setup import apply_gog_setup
        await apply_gog_setup(plan, _install_language(work_dir) or "en-US")
    except Exception:
        logger.exception("[compat.gog] gog_setup failed (non-fatal)")


async def _run_gog_with_fallback(
    plan: ProtonLaunchPlan, work_dir: Path,
) -> int:
    """Run via Comet; retry the real game exe if a launcher stub bails early."""
    comet = start_comet(plan)
    try:
        start = time.monotonic()
        rc = await _run_umu_exe(plan, plan.context.exe_path, work_dir)
        elapsed = time.monotonic() - start
        if rc != 0 and elapsed < EARLY_EXIT_SECONDS:
            fallback = resolve_fallback_exe(str(work_dir))
            if fallback and fallback != str(plan.context.exe_path):
                logger.info(
                    "[compat.gog] launcher stub exited in %ds (rc=%d), "
                    "retrying game exe: %s", int(elapsed), rc, fallback,
                )
                rc = await _run_umu_exe(
                    plan, Path(fallback), work_dir, max_attempts=1,
                )
        return rc
    finally:
        if comet is not None:
            # Wait for Comet to upload the session's unlocks and self-exit
            # (bounded) BEFORE this launch returns — so GAME_STOPPED, and the
            # post-play achievement reconcile it triggers, see the new unlocks.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_shutdown_comet, comet)
