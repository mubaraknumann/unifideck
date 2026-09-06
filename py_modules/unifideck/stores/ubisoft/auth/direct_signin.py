"""
Direct sign-in fallback — re-use credentials from an already-authed UPC install.

If the user already has UPC installed (e.g. from a previous Unifideck
install or a manually-installed Heroic) the credentials may already be
present in some Wine prefix. ``_DirectSignIn`` scans known prefix
locations, looks for valid credential files, and — if found — short-
circuits the full shortcut-based auth flow by importing those
credentials directly into the auth prefix.

This makes "sign in" effectively instant for returning users.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from unifideck.security import emit_external_auth_check_failed
from unifideck.stores.ubisoft.binaries import UbisoftBinaryResolver
from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths
from unifideck.stores.ubisoft.session import UbisoftSession

logger = logging.getLogger(__name__)

class _DirectSignIn:
    """Direct sign in."""

    def __init__(
        self,
        *,
        binaries: UbisoftBinaryResolver,
        bus: Any,
        config: Any,
        paths: UbisoftPrefixPaths,
        session: UbisoftSession,
        ensure_auth_prefix: Any,
        queue_auth_assets_ensure: Any,
    ) -> None:
        """Initialize the instance."""
        self._binaries = binaries
        self._bus = bus
        self._config = config
        self._paths = paths
        self._session = session
        self._ensure_auth_prefix = ensure_auth_prefix
        self._queue_auth_assets_ensure = queue_auth_assets_ensure

    async def connect(self) -> dict[str, Any]:
        """Connect."""
        self._queue_auth_assets_ensure("connect-account")
        resolved = await self._resolve_launch_targets()
        if isinstance(resolved, dict):
            return resolved
        umu_run, connect_path, prefix_path = resolved
        python_bin, env = self._build_launch_env(prefix_path)
        logger.info(
            "[UbisoftAuth] launching Ubisoft Connect in auth prefix for login",
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                python_bin,
                umu_run,
                connect_path,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as e:
            return {
                "success": False,
                "error": f"upc_spawn_failed: {e}",
            }
        captured_token = await self._wait_for_capture(
            proc,
            prefix_path,
        )
        if not captured_token:
            captured_token = self._session.capture(prefix_path)
        if captured_token:
            return self._finalize_success(prefix_path)
        return {
            "success": False,
            "error": ("Login not detected. Please log in and close Ubisoft Connect."),
        }

    async def _resolve_launch_targets(
        self,
    ) -> tuple[str, str, str] | dict[str, Any]:
        """Resolve launch targets."""
        upc_path = await self._ensure_auth_prefix()
        umu_run = self._binaries.find_umu_run()
        if not upc_path or not umu_run:
            emit_external_auth_check_failed(
                self._bus,
                "ubisoft",
                "upc_not_found",
                "Ubisoft Connect exe missing from auth prefix",
            )
            return {
                "success": False,
                "error": "upc_not_found_in_auth_prefix",
            }
        prefix_path = self._config.auth_prefix_dir_expanded
        connect_path = self._paths.find_connect_exe(prefix_path)
        if not connect_path:
            emit_external_auth_check_failed(
                self._bus,
                "ubisoft",
                "connect_exe_not_found",
                "find_connect_exe returned empty",
            )
            return {
                "success": False,
                "error": "ubisoft_connect_exe_not_found",
            }
        return umu_run, connect_path, prefix_path

    def _build_launch_env(
        self,
        prefix_path: str,
    ) -> tuple[str, dict[str, str]]:
        """Build launch env."""
        python_bin = self._binaries.find_python()
        env = self._binaries.build_umu_env(
            wineprefix=prefix_path,
            gameid="umu-ubisoft-auth",
            store_game_id=self._config.auth_shortcut_store_id,
        )
        return python_bin, env

    def _finalize_success(
        self,
        prefix_path: str,
    ) -> dict[str, Any]:
        """Finalize success."""
        self._session.propagate_all_to_all()
        self._queue_auth_assets_ensure("post-connect-account")
        return {
            "success": True,
            "message": "Ubisoft account connected successfully",
        }

    async def _wait_for_capture(
        self,
        proc: asyncio.subprocess.Process,
        prefix_path: str,
    ) -> str | None:
        """Wait for capture."""
        loop = asyncio.get_event_loop()
        start = loop.time()
        timeout_seconds = 600.0
        captured: str | None = None
        while loop.time() - start < timeout_seconds:
            if proc.returncode is not None:
                break
            captured = self._session.capture(prefix_path)
            if captured:
                logger.info(
                    "[UbisoftAuth] UPC session captured during auth; closing launcher",
                )
                try:
                    proc.terminate()
                    await asyncio.wait_for(
                        proc.wait(),
                        timeout=10,
                    )
                except (TimeoutError, ProcessLookupError):
                    with contextlib.suppress(TimeoutError, ProcessLookupError):
                        proc.kill()
                        await asyncio.wait_for(
                            proc.wait(),
                            timeout=5,
                        )
                return captured
            await asyncio.sleep(2)
        logger.warning(
            "[UbisoftAuth] auth launcher timed out",
        )
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=10)
        except (TimeoutError, ProcessLookupError):
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
        return None
