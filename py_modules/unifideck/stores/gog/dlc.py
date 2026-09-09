"""GOG DLC enumeration and installation.

``GOGDlcManager`` handles the DLC lifecycle for a parent game :

* enumerate DLCs owned for a given game id (queries GOG.com);
* identify which DLCs are already installed on disk;
* install / uninstall a DLC through ``gogdl`` (the GOG CLI);
* report per-DLC progress through the standard event bus.

DLCs share the parent game's install directory and prefix; this manager
just adds/removes the DLC-specific files via gogdl while leaving the
parent install in place.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from unifideck.core.types import Result

from .config import GOGConfig
from .http import fetch_json_get
from .tokens import GOGTokenManager

logger = logging.getLogger(__name__)
_LANGUAGE_FALLBACK = ["en-US"]
_LANG_PROBE_TIMEOUT_S = 30.0

class GOGDlcManager:
    """Gogdlc manager."""

    def __init__(
        self,
        config: GOGConfig,
        tokens: GOGTokenManager,
        gogdl_bin: str,
        locale_fn: Callable[[], str],
        resolve_install_path: Callable[
            [str],
            dict[str, str | None] | None,
        ],
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._tokens = tokens
        self._gogdl_bin = gogdl_bin
        self._locale_fn = locale_fn
        self._resolve_install = resolve_install_path

    async def get_game_dlcs(self, game_id: str) -> list[dict[str, Any]]:
        """Get game dlcs."""
        if not await self._tokens.refresh_if_stale():
            logger.warning(
                "[GOGDlcManager] not authenticated for DLC fetch",
            )
            return []
        access = self._tokens.access_token
        if not access:
            return []
        product_url = (
            f"{self._config.api_gog_url}/products/{game_id}"
            f"?expand=downloads&locale=en-US"
        )
        product = await self._http_get_json(
            product_url,
            bearer=access,
        )
        if not isinstance(product, dict):
            return []
        dlcs_info = product.get("dlcs", {})  # type: ignore[unreachable]  # defensive guard on subprocess output
        if not isinstance(dlcs_info, dict) or not dlcs_info:
            logger.debug(
                "[GOGDlcManager] no DLCs for %s",
                game_id,
            )
            return []
        expanded_url = dlcs_info.get(
            "expanded_all_products_url",
        )
        if isinstance(expanded_url, str) and expanded_url:
            expanded = await self._http_get_json(
                expanded_url,
                bearer=access,
            )
            if isinstance(expanded, list):
                logger.info(
                    "[GOGDlcManager] found %d DLCs for %s",
                    len(expanded),
                    game_id,
                )
                return expanded
            logger.warning(
                "[GOGDlcManager] expanded DLC list malformed for %s",
                game_id,
            )
        basic_products = dlcs_info.get("products", [])
        if isinstance(basic_products, list):
            return basic_products
        return []

    async def get_available_languages(self, game_id: str) -> list[str]:
        """Get available languages.

        Mirrors the installer's platform resolution (Linux build
        preferred, Windows fallback — see
        ``_InstallHelpers.probe_game_info``) so the languages shown
        in the install modal match the build that actually installs.
        A Windows-only probe would otherwise list a different set
        than the resolved build, breaking the modal pre-selection
        and the language pass-through.
        """
        for platform in ("linux", "windows"):
            stdout, returncode = await self._spawn_lang_probe(game_id, platform)
            if returncode is None:
                # Fatal pre-check (no tokens / gogdl missing) — not
                # platform-specific, so don't bother with Windows.
                return list(_LANGUAGE_FALLBACK)
            if returncode != 0:
                if platform == "linux":
                    logger.info(
                        "[GOGDlcManager] no Linux build for %s, trying Windows",
                        game_id,
                    )
                    continue
                return list(_LANGUAGE_FALLBACK)
            languages = self._parse_languages_from_info(stdout or b"")
            if languages:
                logger.info(
                    "[GOGDlcManager] %s (%s) languages: %s",
                    game_id,
                    platform,
                    languages,
                )
                return languages
            logger.warning(
                "[GOGDlcManager] no languages in info output for %s (%s)",
                game_id,
                platform,
            )
            return list(_LANGUAGE_FALLBACK)
        return list(_LANGUAGE_FALLBACK)

    async def _spawn_lang_probe(
        self,
        game_id: str,
        platform: str,
    ) -> tuple[bytes | None, int | None]:
        """Run ``gogdl info`` for ``platform``.

        Returns ``(stdout, returncode)``. ``returncode`` is ``None``
        for a fatal pre-check failure (no tokens / gogdl missing)
        that won't differ by platform, so the caller can stop early
        rather than retrying the other platform.
        """
        if not await self._tokens.refresh_if_stale():
            return None, None
        if not await asyncio.to_thread(lambda: Path(self._gogdl_bin).is_file()):
            return None, None
        try:
            async with self._tokens.gogdl_credentials() as (env, creds_path):
                cmd = [
                    self._gogdl_bin,
                    "--auth-config-path",
                    creds_path,
                    "info",
                    "--platform",
                    platform,
                    game_id,
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=_LANG_PROBE_TIMEOUT_S,
                )
        except TimeoutError:
            logger.warning(
                "[GOGDlcManager] language probe (%s) timed out for %s",
                platform,
                game_id,
            )
            return None, None
        except OSError as e:
            logger.warning(
                "[GOGDlcManager] gogdl spawn failed: %s",
                e,
            )
            return None, None
        return stdout, proc.returncode

    @staticmethod
    def _parse_languages_from_info(stdout: bytes) -> list[str]:
        """Parse languages from info."""
        for raw_line in stdout.decode(
            errors="replace",
        ).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            langs = data.get("languages")
            if isinstance(langs, list) and langs:
                result = [str(x) for x in langs if x]
                if result:
                    return result
        return []

    async def install_dlc(
        self,
        game_id: str,
        dlc_id: str,
        base_path: str | None = None,
        progress_cb: (Callable[[dict[str, Any]], Awaitable[None]] | None) = None,
    ) -> Result:
        """Install dlc."""
        failure = await self._dlc_preflight()
        if failure is not None:
            return failure
        resolved_base = self._dlc_resolve_base_path(
            game_id,
            base_path,
        )
        preferred_lang = self._locale_fn() or "en-US"
        logger.info(
            "[GOGDlcManager] installing DLC %s for game %s at %s (lang=%s)",
            dlc_id,
            game_id,
            resolved_base,
            preferred_lang,
        )
        proc = await self._dlc_spawn_gogdl(
            dlc_id,
            resolved_base,
            preferred_lang,
        )
        if proc is None:
            return Result(
                success=False,
                error="gogdl_spawn_failed",
            )
        await self._dlc_read_loop(proc, dlc_id, progress_cb)
        return await self._dlc_finalize(proc, dlc_id)

    async def _dlc_preflight(self) -> Result | None:
        """Dlc preflight."""
        if not await asyncio.to_thread(lambda: Path(self._gogdl_bin).is_file()):
            return Result(
                success=False,
                error="gogdl_not_found",
            )
        if not await self._tokens.refresh_if_stale():
            return Result(
                success=False,
                error="not_authenticated",
            )
        return None

    def _dlc_resolve_base_path(self, game_id: str, base_path: str | None) -> str:
        """Dlc resolve base path."""
        if base_path:
            return base_path
        info = self._resolve_install(game_id)
        if info and isinstance(info.get("install_path"), str):
            return cast("str", info["install_path"])
        return str(
            Path(self._config.download_dir).expanduser(),
        )

    async def _dlc_spawn_gogdl(
        self,
        dlc_id: str,
        base_path: str,
        lang: str,
    ) -> asyncio.subprocess.Process | None:
        """Dlc spawn GOGDL."""
        try:
            env, creds_path, cleanup = await self._tokens.acquire_gogdl_creds()
            cmd = [
                self._gogdl_bin,
                "--auth-config-path",
                creds_path,
                "repair",
                dlc_id,
                "--platform",
                "windows",
                "--path",
                base_path,
                "--lang",
                lang,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            proc._unifideck_gogdl_cleanup = cleanup  # type: ignore[attr-defined]  # Process._unifideck_gogdl_cleanup added at spawn time
            return proc
        except OSError:
            logger.exception("[GOGDlcManager] gogdl spawn failed")
            return None

    async def _dlc_read_loop(
        self,
        proc: asyncio.subprocess.Process,
        dlc_id: str,
        progress_cb: (Callable[[dict[str, Any]], Awaitable[None]] | None),
    ) -> None:
        """Dlc read loop."""
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line_str = line.decode(errors="replace").strip()
            if not line_str:
                continue
            if progress_cb is not None and "Progress:" in line_str:
                await self._forward_dlc_progress(
                    line_str,
                    dlc_id,
                    progress_cb,
                )

    async def _dlc_finalize(
        self,
        proc: asyncio.subprocess.Process,
        dlc_id: str,
    ) -> Result:
        """Dlc finalize."""
        await proc.wait()
        if proc.returncode != 0:
            logger.error(
                "[GOGDlcManager] DLC install failed (code %d)",
                proc.returncode,
            )
            return Result(
                success=False,
                error=(f"dlc_install_failed_code_{proc.returncode}"),
            )
        logger.info(
            "[GOGDlcManager] DLC %s installed successfully",
            dlc_id,
        )
        return Result(success=True)

    @staticmethod
    async def _forward_dlc_progress(
        line_str: str,
        dlc_id: str,
        progress_cb: Callable[
            [dict[str, Any]],
            Awaitable[None],
        ],
    ) -> None:
        """Forward dlc progress."""
        try:
            part = line_str.split("Progress:", 1)[1].strip()
            tokens = part.split()
            if not tokens:
                return
            percent = float(tokens[0])
            await progress_cb(
                {
                    "progress_percent": percent,
                    "dlc_id": dlc_id,
                }
            )
        except (ValueError, IndexError) as e:
            logger.debug(
                "[GOGDlcManager] DLC progress parse: %s",
                e,
            )

    async def get_game_store_url(self, game_id: str) -> str | None:
        """Get game store URL."""
        url = f"{self._config.api_gog_url}/products/{game_id}?expand=description"
        data = await self._http_get_json(url)
        if not isinstance(data, dict):
            return None
        links = data.get("links", {})  # type: ignore[unreachable]  # defensive guard on subprocess output
        if not isinstance(links, dict):
            return None
        product_card = links.get("product_card")
        if isinstance(product_card, str) and product_card:
            return product_card
        return None

    async def _http_get_json(
        self,
        url: str,
        bearer: str | None = None,
    ) -> asyncio.subprocess.Process | None:
        """Http get JSON."""
        return await fetch_json_get(
            url,
            bearer=bearer,
            user_agent=self._config.user_agent,
            timeout=10.0,
            log_prefix="[GOGDlcManager]",
        )
