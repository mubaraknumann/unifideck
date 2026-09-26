from __future__ import annotations

import logging
import shutil
import subprocess
import time
from urllib.parse import urlparse

from unifideck.cdp.page_inject import inject_scripts
from unifideck.cdp.xcloud_browser_shims import (
    get_xcloud_browser_shims_js,
    get_xcloud_navigation_js,
)

from .steam_controller_popup import refresh_steam_controller_layout

logger = logging.getLogger(__name__)
def _build_launch_matches(launch_url: str) -> list[str]:
    """Build launch matches."""
    if not launch_url:
        return []
    parsed = urlparse(launch_url)
    path = parsed.path.rstrip("/")
    product_id = path.split("/")[-1] if path else ""
    matches: list[str] = [launch_url]
    if path:
        matches.append(path)
    if product_id:
        matches.append(product_id)
        matches.append(f"/play/launch/{product_id}")
    deduped: list[str] = []
    for match in matches:
        if match and match not in deduped:
            deduped.append(match)
    return deduped
def _merge_matches(*match_sets: list[str]) -> list[str]:
    """Merge matches."""
    merged: list[str] = []
    for match_set in match_sets:
        for match in match_set:
            if match and match not in merged:
                merged.append(match)
    return merged

def _focus_xcloud_window() -> None:

    """Focus xcloud window."""
    if shutil.which("xdotool") is None:
        return
    search_commands = [
        ["xdotool", "search", "--onlyvisible", "--classname", "unifideck-browser-game"],
        ["xdotool", "search", "--onlyvisible", "--classname", "www.xbox.com__play"],
        [
            "xdotool", "search", "--onlyvisible", "--name",
            "Xbox Cloud Gaming|xbox.com",
        ],
    ]
    for _ in range(20):
        for command in search_commands:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            window_ids = [
                line.strip() for line in result.stdout.splitlines()
                if line.strip()
            ]
            if not window_ids:
                continue
            window_id = window_ids[-1]
            for activate_cmd in (
                ["xdotool", "windowactivate", "--sync", window_id],
                ["xdotool", "windowraise", window_id],
                ["xdotool", "windowfocus", window_id],
            ):
                subprocess.run(
                    activate_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=3,
                )
            logger.info("[xcloud_cdp] refocused xCloud window %s", window_id)
            return
        time.sleep(0.25)
async def run_cdp_inject(
    *,
    port: int,
    launch_url: str,
    timeout: float = 45.0,  # noqa: ASYNC109 — timeout is API value passed to underlying lib (urllib/aiohttp/subprocess), not an asyncio.timeout() wrapper
    initial_matches: list[str] | None = None,
    steam_port: int = 8080,
    steam_controller_appid: int = 0,
    steam_controller_delay: float = 10.0,
    steam_controller_dwell: float = 2.5,
) -> bool:
    """Run CDP inject."""
    shims_js = get_xcloud_browser_shims_js()
    navigation_js = (
        get_xcloud_navigation_js(launch_url) if launch_url else ""
    )
    if not await _run_inject_phases(
        port, timeout,
        shims_js=shims_js,
        navigation_js=navigation_js,
        launch_url=launch_url,
        initial_matches=initial_matches,
    ):
        return False
    if steam_controller_appid > 0:
        return await refresh_steam_controller_layout(
            steam_port,
            steam_controller_appid,
            delay=steam_controller_delay,
            dwell=steam_controller_dwell,
            on_complete=_focus_xcloud_window,
        )
    return True

async def _run_inject_phases(
    port: int,
    timeout: float,  # noqa: ASYNC109 — timeout is API value passed to underlying lib (urllib/aiohttp/subprocess), not an asyncio.timeout() wrapper
    *,
    shims_js: str,
    navigation_js: str,
    launch_url: str,
    initial_matches: list[str] | None,
) -> bool:

    """Run inject phases."""
    final_matches = _build_launch_matches(launch_url)
    base_matches = initial_matches if initial_matches else ["xbox.com"]
    first_pass_matches = _merge_matches(base_matches, final_matches)
    initial_sources = [shims_js]
    if navigation_js:
        initial_sources.append(navigation_js)
    ok = await inject_scripts(
        port,
        initial_sources,
        url_patterns=first_pass_matches,
        timeout=timeout,
        logger_prefix="xcloud-cdp",
    )
    if not ok:
        logger.warning("[xcloud_cdp] initial shim inject failed")
        return False
    if launch_url:
        targeted_sources = (
            [shims_js, navigation_js] if navigation_js else [shims_js]
        )
        ok = await inject_scripts(
            port,
            targeted_sources,
            url_patterns=final_matches,
            timeout=timeout,
            logger_prefix="xcloud-cdp-final",
        )
        if not ok:
            logger.warning("[xcloud_cdp] targeted shim inject failed")
            return False
    return True
