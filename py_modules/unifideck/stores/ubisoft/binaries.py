"""
UPC binary resolution — find the right executable for the install in a prefix.

``UbisoftBinaryResolver`` locates the Ubisoft Connect / UPC executable
inside a Wine prefix. Modern UPC ships ``UbisoftConnect.exe`` (Electron-
based UI) and a legacy ``upc.exe`` (the original launcher); newly
installed UPC may have only ``UbisoftConnect.exe`` while older installs
have both — the resolver picks the modern one when available and falls
back to the legacy binary otherwise.

It also exposes helpers to:

* probe a prefix for *any* working UPC binary;
* validate a candidate path against config-declared relative paths;
* enumerate every game-install binary candidate found in the prefix's
  ``games/`` sub-directory.

Returns ``str`` paths to maintain compatibility with subprocess callers
that pass paths directly to ``proton run``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from unifideck.launcher.proton.infrastructure.umu_runtime import (
    repair_incomplete_umu_runtime,
)

from .config import UbisoftConfig

logger = logging.getLogger(__name__)
_DISPLAY_ENV_VARS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XAUTHORITY",
)
_PROTON_OFFICIAL_NAMES = (
    "Proton - Experimental",
    "Proton 10.0",
    "Proton 9.0 (Beta)",
)
_STEAM_COMMON_CANDIDATES = (
    str(Path("~") / ".steam" / "steam" / "steamapps" / "common"),
    str(
        Path("~") / ".local" / "share" / "Steam" / "steamapps" / "common",
    ),
    str(Path("~") / ".steam" / "root" / "steamapps" / "common"),
)
_COMPAT_TOOLS_DIR = "~/.local/share/Steam/compatibilitytools.d"

class UbisoftBinaryResolver:
    """Ubisoft binary resolver."""

    def __init__(
        self,
        config: UbisoftConfig,
        plugin_dir: str | None,
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._plugin_dir = plugin_dir

    def find_umu_run(self) -> str | None:
        """Find UMU run.

        Self-heals a half-downloaded umu runtime (payload present but the
        umu/_v2-entry-point link missing, UD-084) before anything spawns
        umu-run from this resolver. The out-of-process game launcher's
        ``dispatch()`` already does this for real launches; every
        backend-side (in-process) umu-run spawn for Ubisoft goes through
        this resolver, so this is the single choke point for the auth-prefix
        / template / per-game silent-install paths too. Store-agnostic and
        a cheap no-op stat when the runtime is healthy.
        """
        repair_incomplete_umu_runtime()
        candidates: list[str] = []
        if self._plugin_dir:
            candidates.append(
                str(
                    Path(self._plugin_dir) / "bin" / "umu" / "umu" / "umu-run",
                )
            )
        candidates.extend(
            [
                str(
                    Path(
                        "~/.local/share/unifideck/bin/umu/umu/umu-run",
                    ).expanduser()
                ),
                "/usr/bin/umu-run",
            ]
        )
        for path in candidates:
            if Path(path).is_file():
                return path
        logger.warning("[UbisoftBinaryResolver] umu-run not found")
        return None

    def find_proton_path(self) -> str | None:
        """Find PROTON path.

        The plugin-managed GE-Proton first, because a prefix must be *built*
        by the Proton that will later *run* it. This resolver only ever
        serves backend-side umu spawns — a vendor client's installer, a
        prefix warmup — while every launch of that same prefix goes through
        the launcher's own selector, which picks managed GE. Those two
        disagreeing is how a Battle.net prefix came to be created by
        ``Proton - Experimental`` and driven by ``GE-Proton11-5``.

        The disagreement was not a choice anyone made. It came out of
        :data:`_PROTON_OFFICIAL_NAMES`, a hardcoded display-name table that
        stopped at Proton 10: on a host with Proton 11.0, GE-Proton11-5,
        GE-Proton10-34, Proton-CachyOS and EM-10.0-34 installed, the only
        name it could still match was Experimental. That is the same rot
        ``vdf_compat.official_proton_alias`` was written to end, and the
        cached-tag lookup below has no table to go stale.

        Cached-tag only: never ``ensure_latest_ge``. This runs in the
        backend, where a hundreds-of-MB Proton download would stall an
        install or a sign-in with nothing on screen to explain it. When no
        validated tag is on disk the old scan still answers.
        """
        managed = self._find_managed_ge_proton()
        if managed is not None:
            return managed
        official = self._find_official_proton()
        if official is not None:
            return official
        custom = self._find_custom_proton()
        if custom is not None:
            return custom
        logger.warning(
            "[UbisoftBinaryResolver] no Proton found in "
            "steamapps/common or compatibilitytools.d",
        )
        return None

    @staticmethod
    def _find_managed_ge_proton() -> str | None:
        """The GE-Proton the plugin installed and validated, if it is on disk.

        Returns the tool *directory*: ``installed_ge_proton_path`` answers
        with the ``proton`` script inside it, and ``PROTONPATH`` wants the
        directory. Getting that wrong hands umu a path it cannot use.
        """
        try:
            from unifideck.launcher.proton.infrastructure import ge_installer

            tag = ge_installer.read_cached_latest_tag()
            script = ge_installer.installed_ge_proton_path(tag) if tag else None
        except Exception:
            logger.debug(
                "[UbisoftBinaryResolver] managed GE-Proton lookup failed",
                exc_info=True,
            )
            return None
        if script is None:
            return None
        logger.info("[UbisoftBinaryResolver] using Proton: %s (plugin-managed)", tag)
        return str(script.parent)

    @staticmethod
    def _find_official_proton() -> str | None:
        """Find official PROTON."""
        for steam_common_raw in _STEAM_COMMON_CANDIDATES:
            steam_common = Path(steam_common_raw).expanduser()
            for name in _PROTON_OFFICIAL_NAMES:
                candidate = steam_common / name
                if candidate.is_dir():
                    logger.info(
                        "[UbisoftBinaryResolver] using Proton: %s",
                        name,
                    )
                    return str(candidate)
        return None

    @staticmethod
    def _find_custom_proton() -> str | None:
        """Find custom PROTON."""
        compat_dir = Path(_COMPAT_TOOLS_DIR).expanduser()
        if not compat_dir.is_dir():
            return None
        umu_candidates: list[str] = []
        ge_candidates: list[str] = []
        try:
            for entry in compat_dir.iterdir():
                if not entry.is_dir():
                    continue
                if entry.name.startswith("UMU-Proton"):
                    umu_candidates.append(str(entry))
                elif entry.name.startswith("GE-Proton"):
                    ge_candidates.append(str(entry))
        except OSError as e:
            logger.warning(
                "[UbisoftBinaryResolver] compatibilitytools.d scan failed: %s",
                e,
            )
            return None
        ge_candidates.sort(
            key=lambda p: Path(p).name,
            reverse=True,
        )
        ordered = umu_candidates + ge_candidates
        if not ordered:
            return None
        logger.info(
            "[UbisoftBinaryResolver] using Proton: %s",
            Path(ordered[0]).name,
        )
        return ordered[0]

    @staticmethod
    def find_python() -> str:
        """Find python."""
        for name in ("python3", "python"):
            path = shutil.which(name)
            if path:
                return path
        return "python3"

    @staticmethod
    def proton_family(version_str: str) -> str:
        """Proton family."""
        v = (version_str or "").lower()
        if "umu-proton" in v:
            return "umu-proton"
        if "ge-proton" in v:
            return "ge-proton"
        if "experimental" in v:
            return "experimental"
        stripped = v.replace(".", "").replace("-", "").replace(" ", "")
        if stripped.isdigit():
            return "experimental"
        return "other"

    def build_umu_env(
        self,
        wineprefix: str,
        gameid: str,
        *,
        proton_path: str | None = None,
        store_game_id: str | None = None,
        steam_window_env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build UMU env."""
        home = os.environ.get(
            "HOME",
            str(Path("~").expanduser()),
        )
        uid = os.getuid()
        env: dict[str, str] = {
            "HOME": home,
            "USER": os.environ.get("USER", "deck"),
            "PATH": os.environ.get(
                "PATH",
                "/usr/local/bin:/usr/bin:/bin",
            ),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "XDG_RUNTIME_DIR": os.environ.get(
                "XDG_RUNTIME_DIR",
                f"/run/user/{uid}",
            ),
            "XDG_DATA_HOME": os.environ.get(
                "XDG_DATA_HOME",
                str(Path(home) / ".local" / "share"),
            ),
            "WINEPREFIX": wineprefix,
            "GAMEID": gameid,
            "STORE": "ubisoft",
            "PROTON_VERB": "waitforexitandrun",
        }
        if steam_window_env:
            env.update(steam_window_env)
        if proton_path is None:
            proton_path = self.find_proton_path()
        if proton_path:
            env["PROTONPATH"] = proton_path
        env.update(self.detect_display_env())
        return env

    def detect_display_env(self) -> dict[str, str]:
        """Detect display env."""
        result = self._collect_display_env_from_self()
        if result.get("DISPLAY") or result.get("WAYLAND_DISPLAY"):
            return result
        if self._fill_display_env_from_steam(result):
            return result
        self._apply_steam_deck_defaults(result)
        return result

    @staticmethod
    def _collect_display_env_from_self() -> dict[str, str]:
        """Collect display env from self."""
        result: dict[str, str] = {}
        for var in _DISPLAY_ENV_VARS:
            val = os.environ.get(var)
            if val:
                result[var] = val
        return result

    def _fill_display_env_from_steam(
        self,
        result: dict[str, str],
    ) -> bool:
        """Fill display env from steam."""
        try:
            for proc_name in ("steam", "gamescope-session"):
                for pid in self._pgrep(proc_name):
                    if self._scan_pid_for_display(pid, result):
                        logger.info(
                            "[UbisoftBinaryResolver] display "
                            "env detected from PID %s (%s)",
                            pid,
                            proc_name,
                        )
                        return True
        except Exception as e:
            logger.debug(
                "[UbisoftBinaryResolver] display env detection: %s",
                e,
            )
        return False

    def _scan_pid_for_display(
        self,
        pid: str,
        result: dict[str, str],
    ) -> bool:
        """Scan pid for display."""
        env_from_proc = self._read_proc_environ(
            pid,
            _DISPLAY_ENV_VARS,
        )
        if not env_from_proc:
            return False
        for k, v in env_from_proc.items():
            result.setdefault(k, v)
        return bool(
            result.get("DISPLAY") or result.get("WAYLAND_DISPLAY"),
        )

    @staticmethod
    def _apply_steam_deck_defaults(
        result: dict[str, str],
    ) -> None:
        """Apply steam DECK defaults."""
        if not result.get("DISPLAY"):
            result["DISPLAY"] = ":0"
            logger.info(
                "[UbisoftBinaryResolver] using fallback DISPLAY=:0",
            )
        xauth = Path("~").expanduser() / ".Xauthority"
        if not result.get("XAUTHORITY") and xauth.is_file():
            result["XAUTHORITY"] = str(xauth)

    @staticmethod
    def _pgrep(process_name: str) -> list[str]:
        """Pgrep."""
        try:
            result = subprocess.run(
                [
                    "pgrep",
                    "-u",
                    str(os.getuid()),
                    "-x",
                    process_name,
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return []
        return [
            line.strip() for line in result.stdout.strip().splitlines() if line.strip()
        ]

    @staticmethod
    def _read_proc_environ(
        pid: str,
        targets: tuple[str, ...],
    ) -> dict[str, str]:
        """Read proc environ."""
        try:
            env_path = Path(f"/proc/{pid}/environ")
            env_bytes = env_path.read_bytes()
        except (PermissionError, FileNotFoundError, OSError):
            return {}
        result: dict[str, str] = {}
        for entry in env_bytes.split(b"\x00"):
            decoded = entry.decode("utf-8", errors="replace")
            if "=" not in decoded:
                continue
            k, v = decoded.split("=", 1)
            if k in targets:
                result[k] = v
        return result
