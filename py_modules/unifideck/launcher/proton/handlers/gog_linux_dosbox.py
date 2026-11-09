from __future__ import annotations

import logging
import os
import platform
import re
import shlex
import sys
from pathlib import Path
from typing import NoReturn

logger = logging.getLogger(__name__)
DOSBOX_CALL_RE = re.compile(r'run_dosbox\s+((?:\"[^\"]+\"\s*)+)')
def find_steam_runtime() -> Path | None:
    """The scout Steam Runtime tree whose libraries GOG's DOSBox builds need.

    Current Steam clients ship ``ubuntu12_32/steam-runtime`` as a 68 KB stub
    (``legacy-run-stub.sh``) with no libraries, so GOG's bundled DOSBox died
    on ``libFLAC.so.8``. The same scout tree still ships inside the Steam
    Linux Runtime 1.0 tool. A candidate counts only if it holds a library
    directory, never merely because the folder exists.
    """
    homes = (Path.home() / ".steam" / "steam", Path.home() / ".local" / "share" / "Steam")
    candidates = [h / "ubuntu12_32" / "steam-runtime" for h in homes]
    candidates += [
        h / "steamapps" / "common" / "SteamLinuxRuntime" / "steam-runtime"
        for h in homes
    ]
    for candidate in candidates:
        if (candidate / "usr" / "lib" / "x86_64-linux-gnu").is_dir():
            return candidate
    return None
def build_runtime_library_paths(
    runtime_root: Path, arch_dir: str,
) -> list[str]:
    """Build runtime library paths."""
    paths: list[str] = []
    for rel in (f"usr/lib/{arch_dir}", f"lib/{arch_dir}"):
        candidate = runtime_root / rel
        if candidate.exists():
            paths.append(str(candidate))
    return paths
def parse_dosbox_conf_args(start_script: Path) -> list[str]:
    """Return the ``-conf`` args to pass to the bundled DOSBox binary.

    GOG's Linux DOSBox depots ship the actual ``.conf`` files as real
    files next to ``start.sh`` — the ``run_dosbox`` shell call inside
    ``start.sh`` just says which of them to use, and in what order.
    Try the shell-parsed order first (it's authoritative when it
    matches); if ``start.sh`` doesn't match the expected phrasing
    (GOG has rephrased this script before), fall back to every
    ``*.conf`` file found directly beside it, alphabetically, so a
    title still gets *a* working config instead of none. Returns an
    empty list (never raises) when neither approach finds anything —
    callers should fall back to running ``start.sh`` directly.
    """
    content = start_script.read_text(encoding="utf-8", errors="ignore")
    match = DOSBOX_CALL_RE.search(content)
    if match:
        return shlex.split(match.group(1))
    globbed = sorted(start_script.parent.glob("*.conf"))
    if globbed:
        logger.info(
            "[gog_linux_dosbox] run_dosbox call not found in %s, "
            "using conf files found beside it: %s",
            start_script, [str(p) for p in globbed],
        )
        return [str(p) for p in globbed]
    logger.warning(
        "[gog_linux_dosbox] no run_dosbox call and no .conf files "
        "found for %s", start_script,
    )
    return []
def launch_via_steam_runtime(
    runtime_root: Path | None,
    start_script: Path,
    args: list[str],
) -> NoReturn:
    """Launch via steam runtime."""
    if runtime_root:
        run_sh = runtime_root / "run.sh"
        if run_sh.exists():
            # ``execv`` (no shell) replaces this Python process with
            # ``run.sh``; running this through a shell would defeat
            # the purpose of the wrapper (extra PID, signal forwarding,
            # quoting hazards).
            os.execv(str(run_sh), [str(run_sh), str(start_script), *args])  # noqa: S606 — exec without shell is the safer pattern
    os.execv(str(start_script), [str(start_script), *args])  # noqa: S606 — exec without shell is the safer pattern
def _parse_argv() -> tuple[Path, list[str]]:
    """Parse argv."""
    if len(sys.argv) < 2:
        raise SystemExit(
            "Usage: python -m "
            "unifideck.launcher.proton.handlers.gog_linux_dosbox "
            "/path/to/start.sh [args...]",
        )
    start_script = Path(sys.argv[1]).resolve()
    extra_args = [
        arg for arg in sys.argv[2:]
        if not re.match(r"^(epic|gog|amazon|ubisoft):", arg)
    ]
    return start_script, extra_args

def _select_architecture(
    dosbox_dir: Path,
) -> tuple[Path, Path, str] | None:

    """Select architecture."""
    arch = platform.machine().lower()
    if arch in {"x86_64", "amd64"}:
        return (
            dosbox_dir / "dosbox_x86_64",
            dosbox_dir / "libs" / "x86_64",
            "x86_64-linux-gnu",
        )
    if arch in {"i686", "i386"}:
        return (
            dosbox_dir / "dosbox_i686",
            dosbox_dir / "libs" / "i686",
            "i386-linux-gnu",
        )
    return None
def _build_env(
    bundled_lib_dir: Path,
    runtime_root: Path | None,
    runtime_arch_dir: str,
) -> dict[str, str]:
    """Build env."""
    runtime_libs = (
        build_runtime_library_paths(runtime_root, runtime_arch_dir)
        if runtime_root else []
    )
    env = os.environ.copy()
    ld_parts = [str(bundled_lib_dir), *runtime_libs]
    if env.get("LD_LIBRARY_PATH"):
        ld_parts.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = ":".join(
        dict.fromkeys(part for part in ld_parts if part),
    )
    return env
def main() -> None:
    """Main."""
    start_script, extra_args = _parse_argv()
    runtime_root = find_steam_runtime()
    if extra_args:
        launch_via_steam_runtime(
            runtime_root, start_script, extra_args,
        )
    root_dir = start_script.parent
    dosbox_dir = root_dir / "dosbox"
    if not dosbox_dir.is_dir():
        launch_via_steam_runtime(
            runtime_root, start_script, extra_args,
        )
    arch_info = _select_architecture(dosbox_dir)
    if arch_info is None:
        launch_via_steam_runtime(
            runtime_root, start_script, extra_args,
        )
    binary, bundled_lib_dir, runtime_arch_dir = arch_info
    if not binary.exists() or not bundled_lib_dir.is_dir():
        launch_via_steam_runtime(
            runtime_root, start_script, extra_args,
        )
    conf_args = parse_dosbox_conf_args(start_script)
    if not conf_args:
        # Neither the run_dosbox shell call nor a bare .conf glob found
        # anything usable — running the bundled binary with no -conf
        # args would launch the generic DOSBox engine with no game
        # config (the exact symptom this module exists to prevent).
        # Falling back to start.sh itself at least runs GOG's own
        # script, which knows its own conf files regardless of our
        # detection.
        launch_via_steam_runtime(
            runtime_root, start_script, extra_args,
        )
    env = _build_env(bundled_lib_dir, runtime_root, runtime_arch_dir)
    command = [str(binary)]
    for conf in conf_args:
        command.extend(["-conf", conf])
    command.extend(["-no-console", "-c", "exit"])
    os.chdir(root_dir)
    # ``execvpe`` (no shell) replaces this Python process with
    # the DOSBox binary; bypassing a shell avoids quoting hazards
    # around the per-game ``.conf`` paths and keeps the env exactly
    # as we built it above.
    os.execvpe(str(binary), command, env)  # noqa: S606 — exec without shell is the safer pattern
if __name__ == "__main__":
    main()
