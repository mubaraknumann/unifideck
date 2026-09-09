"""compat/epic_launch_params.py — resolve Epic's launch recipe from legendary.

UD-126. ``legendary launch`` ends in ``subprocess.Popen(full_params, …)``
with **no** ``wait()`` (see ``legendary/cli.py::launch_game`` inside the
bundled zipapp), so the process we awaited was legendary, not the game —
it returned ~2s after forking umu-run and our launcher exited with it.
Steam then ended the game session while the real game ran on in an
orphaned ``umu-run → steam-runtime-launch-client → bwrap → proton → game``
tree: in Gaming Mode gamescope only raises a window whose app Steam still
believes is running, so a slow-starting title (Rocket League, ~10s) came
up as audio with no window, and Stop/playtime/cloud-sync-up all keyed off
a session that had already "ended".

``legendary launch <id> --json`` returns the resolved ``LaunchParameters``
and exits *before* that ``Popen``, which lets us assemble the identical
command and spawn it ourselves — the same shape ``handlers/generic.py``
already uses for GOG/Amazon/raw-exe. Same exec line, different parent.

legendary's own assembly, reproduced by :func:`build_umu_argv`::

    full_params = launch_command
                + [os.path.join(game_directory, game_executable)]
                + game_parameters + user_parameters + egl_parameters
    env         = os.environ | environment
    cwd         = working_directory

**The resolved parameters carry a secret.** ``egl_parameters`` includes
``-AUTH_PASSWORD=<Epic exchange code>``; never log the assembled argv or
those parameters — count them instead.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.launcher.proton.infrastructure.container_escape import escape_argv
from unifideck.launcher.types.errors import GameFailedError

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)

# Bounds the ``--json`` call. It does the same login + version check +
# ownership-token fetch that ``legendary launch`` does, so it is a network
# call; matches ``compat.epic._LEGENDARY_TIMEOUT_S``.
LEGENDARY_JSON_TIMEOUT_S = 120
# How much of legendary's stderr rides along in the error. legendary's real
# reasons ("Game is out of date, please update or launch with update check
# skipping!", "Login failed, cannot continue!") used to vanish into game.log
# and surface as a bare exit code.
_STDERR_TAIL_LINES = 8


def _stderr_tail(raw: bytes) -> str:
    """Last few non-empty stderr lines, joined for a one-line error."""
    lines = [
        line.strip()
        for line in raw.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    return " | ".join(lines[-_STDERR_TAIL_LINES:])


async def resolve_launch_params(
    argv: list[str],
    env: dict[str, str],
    *,
    timeout: float = LEGENDARY_JSON_TIMEOUT_S,  # noqa: ASYNC109 — bounds a subprocess wait via wait_for, not an asyncio.timeout() wrapper
) -> dict[str, Any]:
    """Run ``legendary launch … --json`` and return the parsed parameters.

    ``argv`` must already carry ``--json`` (see
    ``handlers.epic._build_legendary_argv``). Raises
    :class:`GameFailedError` — carrying legendary's stderr tail — when
    legendary fails, times out, or prints something unusable, because
    every one of those means the launch cannot proceed. There is no
    fallback path on purpose: ``--json`` runs the *same* login/version
    check/entitlement code as ``launch``, so anything that breaks it
    would have broken ``launch`` too.
    """
    logger.info("[compat.epic_params] resolving launch parameters: %s", argv[:4])
    # If Steam wrapped the launcher in its OWN pressure-vessel (the user set
    # Properties > Compatibility on this shortcut), hop out to the host first
    # — legendary used to reach ``_run_umu_once``, which escapes for us, and
    # it must keep running in the same host context as before. No-op when
    # unwrapped.
    escaped = escape_argv(argv, env)
    try:
        proc = await asyncio.create_subprocess_exec(
            *escaped,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise GameFailedError(
            f"could not run legendary: {e}",
            subprocess_rc=1,
            context={"store": "epic"},
        ) from e
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        proc.kill()
        raise GameFailedError(
            f"legendary did not resolve launch parameters within {int(timeout)}s",
            subprocess_rc=1,
            context={"store": "epic", "timeout_s": int(timeout)},
        ) from e
    rc = proc.returncode or 0
    if rc != 0:
        tail = _stderr_tail(stderr)
        raise GameFailedError(
            f"legendary could not launch this game (exit {rc}): {tail}",
            subprocess_rc=rc,
            context={"store": "epic", "legendary_stderr": tail},
        )
    return _parse_params(stdout, stderr)


def _loads_object(text: str) -> Any:
    """Parse legendary's JSON, ignoring anything printed around it.

    Normally stdout is the object alone (legendary logs through
    ``logging``, i.e. stderr). The one path that can add chatter is the
    pressure-vessel escape — when Steam's Force-Compat wraps us,
    ``steam-runtime-launch-client`` sits between legendary and this pipe.
    Falling back to the outermost ``{...}`` span costs nothing and keeps
    that path (which needs a Force-Compat shortcut to exercise) from
    failing on a stray line.
    """
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def _parse_params(stdout: bytes, stderr: bytes) -> dict[str, Any]:
    """Parse legendary's ``--json`` stdout into usable launch parameters.

    legendary logs through ``logging`` (stderr), so stdout is the JSON
    object alone. A payload with no ``launch_command`` or no
    ``game_executable`` is unusable — treat it as a failure rather than
    spawning a half-formed command.
    """
    try:
        data = _loads_object(stdout.decode("utf-8", errors="replace"))
    except ValueError as e:
        raise GameFailedError(
            f"legendary returned unreadable launch parameters: {_stderr_tail(stderr)}",
            subprocess_rc=0,
            context={"store": "epic", "json_error": str(e)},
        ) from e
    if not isinstance(data, dict) or not data.get("game_executable"):
        raise GameFailedError(
            "legendary returned no launchable executable for this game",
            subprocess_rc=0,
            context={"store": "epic", "legendary_stderr": _stderr_tail(stderr)},
        )
    if not data.get("launch_command"):
        # The wrapper we passed IS the launch command; an empty one means
        # legendary dropped it, and running the .exe on the host (no umu,
        # no Proton) would be worse than failing.
        raise GameFailedError(
            "legendary dropped the umu wrapper from the launch command",
            subprocess_rc=0,
            context={"store": "epic"},
        )
    return data


def _str_list(params: dict[str, Any], key: str) -> list[str]:
    """A list-of-strings field from the JSON, tolerating absence/nulls."""
    value = params.get(key) or []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def build_umu_argv(
    plan: ProtonLaunchPlan, params: dict[str, Any],
) -> list[str]:
    """Assemble the exact command ``legendary launch`` would have spawned.

    ``os.path.join`` (not ``Path`` division) is deliberate and mirrors
    ``core.get_launch_parameters``: an *absolute* ``game_executable`` —
    what ``handlers.epic._resolve_exe_override`` produces for a user
    "Change executable" or the Rockstar-on-Epic launch shim — must win
    over ``game_directory`` rather than being appended to it.

    Any user wrappers (Steam launch options) stay in front, matching
    every other store handler.
    """
    exe = os.path.join(
        str(params.get("game_directory") or ""),
        str(params["game_executable"]),
    )
    argv: list[str] = []
    argv.extend(_str_list(params, "launch_command"))
    argv.append(exe)
    argv.extend(_str_list(params, "game_parameters"))
    argv.extend(_str_list(params, "user_parameters"))
    argv.extend(_str_list(params, "egl_parameters"))
    logger.info(
        "[compat.epic_params] launch command ready: exe=%s argc=%d "
        "(game=%d user=%d egl=%d)",
        exe, len(argv), len(_str_list(params, "game_parameters")),
        len(_str_list(params, "user_parameters")),
        len(_str_list(params, "egl_parameters")),
    )
    return argv


def merge_environment(
    env: dict[str, str], params: dict[str, Any],
) -> dict[str, str]:
    """Overlay legendary's env section on ours, as legendary itself does.

    Empty for a normal install — it only carries a user's
    ``[default.env]`` / ``[<app>.env]`` legendary config. Applied last so
    that hand-set config wins, matching ``full_env.update(params.environment)``.
    """
    extra = params.get("environment")
    if not isinstance(extra, dict) or not extra:
        return env
    merged = dict(env)
    merged.update({str(k): str(v) for k, v in extra.items()})
    logger.info(
        "[compat.epic_params] applied %d legendary env override(s)", len(extra),
    )
    return merged


def resolve_cwd(params: dict[str, Any]) -> Path | None:
    """legendary's ``working_directory`` when it exists, else None."""
    raw = str(params.get("working_directory") or "")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


async def maybe_run_pre_launch(
    params: dict[str, Any], env: dict[str, str],
) -> None:
    """Run legendary's ``pre_launch_command``, if the user configured one.

    Never set by Unifideck — it comes from a hand-edited legendary config
    — but ``legendary launch`` honours it, and ``--json`` returns without
    running it, so we run it here to keep the two paths equivalent.
    Best-effort, exactly like legendary: a failing pre-launch command
    never blocks the game.
    """
    command = str(params.get("pre_launch_command") or "").strip()
    if not command:
        return
    try:
        logger.info("[compat.epic_params] running pre-launch command")
        proc = await asyncio.create_subprocess_exec(*shlex.split(command), env=env)
        if params.get("pre_launch_wait"):
            await proc.wait()
    except (OSError, ValueError):
        logger.warning(
            "[compat.epic_params] pre-launch command failed (non-fatal)",
        )
