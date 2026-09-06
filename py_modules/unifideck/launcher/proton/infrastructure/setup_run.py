"""launcher/proton/infrastructure/setup_run.py — run a Windows exe in the prefix.

The one way a prefix-setup step is allowed to execute something inside the
game's Wine prefix: through umu, using the plan's own ``PROTONPATH`` /
``STEAM_COMPAT_DATA_PATH``, with :func:`setup_env.build_setup_env` applying the
setup-helper divergences.

**Never invoke a Proton's ``files/bin/wine`` directly against a prefix.** Proton
builds a prefix whose ``drive_c/windows/system32/*.dll`` entries are SYMLINKS
back into the shared Proton install::

    $ ls -la <prefix>/drive_c/windows/system32/kernel32.dll
    kernel32.dll -> <PROTONPATH>/files/lib/wine/x86_64-windows/kernel32.dll

A bare ``wine`` sees a prefix whose version stamp does not match its own and
runs a full ``wineboot`` prefix update, which reinstalls every builtin PE DLL
into ``system32`` — writing THROUGH those symlinks, into the shared Proton
tree, with source and destination being the same file. Each copy truncates its
own input. That corrupts ``kernel32.dll`` / ``win32u.dll`` / ``user32.dll`` for
every game of every store until the Proton install is deleted and redownloaded.

Measured in a field bundle (Legion Go S, 0.7.4, GE-Proton11-6): one isolated
Ubisoft launch refreshed the mtime of ~1700 files in the shared Proton dir and
left those three DLLs unusable. The launcher log shows the shape — the copy at
``17:04:45.759``, ``wine reg add exited rc=1`` at ``17:04:54.973`` (nine
seconds is a wineboot update, not a ``reg add``), then a ``wineserver --kill``
150 ms later that cut the rewrite off mid-file.

Going through umu avoids all of it: Proton's own script prepares the prefix the
way it expects, so no rogue update runs and nothing is written back through the
symlinks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.launcher.proton.infrastructure.container_escape import escape_argv
from unifideck.launcher.proton.infrastructure.setup_env import build_setup_env

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)


async def run_setup_exe(
    plan: ProtonLaunchPlan,
    exe: str,
    args: list[str],
    *,
    store: str | None = None,
    timeout_s: float | None = None,
    label: str = "setup_run",
) -> bool:
    """Run ``exe`` in the plan's prefix via umu. True on rc 0.

    ``timeout_s`` bounds the wait and kills the child on expiry; pass it for
    anything on the launch hot path. Left ``None`` the wait is unbounded,
    which is what the GOG setup helpers want — an installer that needs four
    minutes should get them rather than be killed halfway through a prefix.

    Setup steps often exit non-zero for benign reasons ("already installed"),
    so callers treat ``False`` as non-fatal.
    """
    env = build_setup_env(plan)
    if store:
        env["STORE"] = store
    # Escape Steam's pressure-vessel when Force-Compat wrapped us, or the
    # step nests a second container and returns rc=1 — observed as 9 straight
    # failures in a field bundle. No-op when unwrapped.
    cmd = escape_argv(
        [str(plan.python_bin), str(plan.umu_wrapper), exe, *args], env, None,
    )
    logger.info("[%s] run: %s %s", label, Path(exe).name, " ".join(args[:4]))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as e:
        logger.warning("[%s] failed to spawn %s: %s", label, exe, e)
        return False
    try:
        rc = (
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            if timeout_s is not None
            else await proc.wait()
        )
    except TimeoutError:
        logger.warning("[%s] %s timed out after %ss", label, exe, timeout_s)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return False
    if rc != 0:
        logger.warning("[%s] command rc=%d (%s)", label, rc, Path(exe).name)
    return rc == 0
