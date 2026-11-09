"""xCloud's session API (``/v5/sessions/cloud``) and the title-id lookup.

Request shapes follow Greenlight's player server (MIT), measured against the
service on 2026-11-03. Azure's gateway in front of it 403s any non-browser
User-Agent, so every call sends a browser one. Blocking: call through
``asyncio.to_thread``.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from unifideck.core.net import ssl_ctx_strict

logger = logging.getLogger(__name__)



def title_cache() -> Path:
    """Where xCloud title ids are cached (resolved at call time)."""
    return Path("~/.local/share/unifideck/xcloud_title_ids.json").expanduser()

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")
_DEVICE_INFO = json.dumps({
    "appInfo": {"env": {"clientAppId": "www.xbox.com", "clientAppType": "browser",
                        "clientAppVersion": "21.1.98", "clientSdkVersion": "8.5.3",
                        "httpEnvironment": "prod", "sdkInstallId": ""}},
    "dev": {"hw": {"make": "Microsoft", "model": "unknown", "sdktype": "web"},
            "os": {"name": "windows", "ver": "22631.2715", "platform": "desktop"},
            "displayInfo": {"dimensions": {"widthInPixels": 1920, "heightInPixels": 1080},
                            "pixelDensity": {"dpiX": 2, "dpiY": 2}}}})
_PLAY_SETTINGS = {
    "nanoVersion": "V3;WebrtcTransport.dll", "enableOptionalDataCollection": False,
    "enableTextToSpeech": False, "highContrast": 0, "locale": "en-US",
    "useIceConnection": False, "timezoneOffsetMinutes": 0, "sdkType": "web", "osName": "windows",
}


def _versions(lo: int, hi: int) -> dict[str, int]:
    return {"minVersion": lo, "maxVersion": hi}


def sdp_body(sdp: str) -> dict[str, Any]:
    """The SDP offer, with the channel versions the player library speaks."""
    return {"messageType": "offer", "sdp": sdp, "requestId": "1", "configuration": {
        "chatConfiguration": {"bytesPerSample": 2, "expectedClipDurationMs": 20,
                              "format": {"codec": "opus", "container": "webm"},
                              "numChannels": 1, "sampleFrequencyHz": 24000},
        "chat": _versions(1, 1), "control": _versions(1, 3), "input": _versions(1, 9),
        "message": _versions(1, 1), "reliableinput": _versions(9, 9),
        "unreliableinput": _versions(9, 9)}}


class XcloudApi:
    """Calls to one xCloud region with one streaming token."""

    def __init__(self, base_uri: str, gs_token: str) -> None:
        self._base = base_uri.rstrip("/")
        self._token = gs_token

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
        """``(status, json body)``; status 0 when no answer arrived. Never raises."""
        req = urllib.request.Request(
            self._base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + self._token, "Accept": "application/json",
                     "Content-Type": "application/json", "X-Gssv-Client": "XboxComBrowser",
                     "X-MS-Device-Info": _DEVICE_INFO, "User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=20, context=ssl_ctx_strict()) as r:
                raw = r.read()
                return r.status, _object(raw)
        except urllib.error.HTTPError as e:
            return e.code, _object(e.read())
        except (OSError, ValueError) as e:
            logger.warning("[xcloud_player] %s %s: no answer (%s)", method, path, type(e).__name__)
            return 0, {}

    def start(self, title_id: str) -> str:
        """Start a cloud session; its path, or ``""``."""
        status, body = self.call("POST", "/v5/sessions/cloud/play", {
            "clientSessionId": "", "titleId": title_id, "systemUpdateGroup": "",
            "settings": _PLAY_SETTINGS, "serverId": "", "fallbackRegionNames": []})
        logger.info("[xcloud_player] start %s -> HTTP %s", title_id, status)
        return str(body.get("sessionPath") or "")

    def exchange(self, session: str, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST an SDP offer or ICE candidates, then poll for the answer."""
        path = f"/{session}/{kind}"
        self.call("POST", path, body)
        for _ in range(60):
            status, answer = self.call("GET", path)
            if status == 200 and answer.get("exchangeResponse"):
                return answer
            time.sleep(0.5)
        return {}

    def title_id(self, product_id: str) -> str:
        """xCloud's title id for a Store product id (``DEATHLOOP`` for ``9P5Z…``)."""
        cache = _read_cache()
        if product_id.upper() in cache:
            return cache[product_id.upper()]
        cont: str | None = None
        for _ in range(20):
            query = "?mr=500" + (f"&ct={urllib.parse.quote(cont)}" if cont else "")
            status, page = self.call("GET", "/v2/titles" + query)
            cache.update(_title_ids(page) if status == 200 else {})
            cont = page.get("continuationToken")
            if product_id.upper() in cache or not cont:
                break
        _write_cache(cache)
        return cache.get(product_id.upper(), "")


def _title_ids(page: dict[str, Any]) -> dict[str, str]:
    """``{PRODUCT_ID: titleId}`` for one page of ``/v2/titles``."""
    found: dict[str, str] = {}
    for item in page.get("results", []):
        pid = str((item.get("details") or {}).get("productId") or "").upper()
        if pid and item.get("titleId"):
            found[pid] = str(item["titleId"])
    return found


def _object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _read_cache() -> dict[str, str]:
    try:
        data = json.loads(title_cache().read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _write_cache(cache: dict[str, str]) -> None:
    try:
        path = title_cache()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache))
    except OSError as e:
        logger.debug("[xcloud_player] title cache not written: %s", e)
