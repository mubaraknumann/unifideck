"""stores/epic/achievements.py — Epic (EGS) achievements (read + normalize).

``EpicAchievements`` fetches a game's achievement definitions AND the user's
unlock status from Epic's (undocumented) storefront GraphQL — the same data
the Epic launcher's profile/achievements pages use — and normalizes it to the
SAME shape the GOG path returns, so the frontend modal renders both identically.

Inputs all come from legendary (no extra resolution): ``epicAccountId`` =
``user.json`` ``account_id``; the launcher OAuth ``access_token`` (refreshed via
``legendary status`` when stale); ``sandboxId`` = the game's legendary metadata
``namespace`` (verified to equal the achievement sandbox).

Two GraphQL ops against ``https://store.epicgames.com/graphql``:
  * ``Achievement(sandboxId, locale)``        — definitions (names/desc/icons).
  * ``PlayerAchievement(epicAccountId, …)``   — per-achievement unlock state.
Merged by the achievement ``name`` (the stable key).

CAVEAT: this is a reverse-engineered, undocumented API (Epic can change it).
Unlocking itself is NOT handled here — Epic games unlock in-game via the EOS
overlay; this is read-only display.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from unifideck.core.net import ssl_ctx_permissive
from unifideck.stores.shared.achievements_error import (
    StoreAchievementsError,
)
from unifideck.stores.shared.timestamps import parse_timestamp

from .launcher_auth import LegendaryLauncherAuth

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://store.epicgames.com/graphql"
# A launcher UA — the storefront GraphQL is the launcher's own backend.
_UA = "EpicGamesLauncher/15.17.1-35104619+++Portal+Release-Live"
_CACHE_TTL_SECONDS = 60.0

# Achievement DEFINITIONS for a sandbox (display names, descriptions, icons).
_DEFS_QUERY = """query Achievement($sandboxId: String!, $locale: String!) {
  Achievement {
    productAchievementsRecordBySandbox(sandboxId: $sandboxId, locale: $locale) {
      totalAchievements
      achievements {
        achievement {
          name
          hidden
          unlockedDisplayName
          lockedDisplayName
          unlockedDescription
          lockedDescription
          unlockedIconLink
          lockedIconLink
          rarity { percent }
        }
      }
    }
  }
}"""

# Per-achievement unlock STATE for (account, sandbox).
_STATE_QUERY = """query PlayerAchievement($epicAccountId: String!, $sandboxId: String!) {
  PlayerAchievement {
    playerAchievementGameRecordsBySandbox(epicAccountId: $epicAccountId, sandboxId: $sandboxId) {
      records {
        totalUnlocked
        playerAchievements {
          playerAchievement { unlocked unlockDate achievementName }
        }
      }
    }
  }
}"""


class EpicAchievementsError(StoreAchievementsError):
    """Typed Epic achievements failure the RPC layer maps to an ``RpcError``."""


class EpicAchievements(LegendaryLauncherAuth):
    """Fetch + normalize + cache an Epic game's achievements (display-only)."""

    _LOG_TAG = "epic.achievements"

    def __init__(
        self,
        cli_path: str | None,
        user_file: str,
        info_timeout: float = 30.0,
    ) -> None:
        """Initialize the instance."""
        self._cli_path = cli_path
        self._user_file = Path(user_file).expanduser()
        self._info_timeout = info_timeout
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def get_game_achievements(
        self, game_id: str, force: bool = False,
    ) -> dict[str, Any]:
        """Return the normalized achievements payload (same shape as GOG)."""
        key = str(game_id)
        if not force:
            entry = self._cache.get(key)
            if entry and (time.monotonic() - entry[0]) < _CACHE_TTL_SECONDS:
                return entry[1]

        token, account_id = await self._resolve_auth()
        if not token or not account_id:
            raise EpicAchievementsError("not_authed")
        sandbox = await asyncio.to_thread(self._resolve_sandbox, game_id)
        if not sandbox:
            raise EpicAchievementsError("no_client_id", game_id=game_id)

        try:
            payload = await asyncio.to_thread(
                self._fetch_blocking, key, account_id, sandbox, token,
            )
        except EpicAchievementsError as e:
            # ``is_available`` only checks the token is PRESENT, not fresh — so
            # a present-but-rejected token reaches here as ``auth_expired``.
            # Force a refresh and retry ONCE before surfacing the error.
            if e.code != "auth_expired":
                raise
            logger.info("[epic.achievements] token rejected — forcing refresh + retry")
            token, account_id = await self._resolve_auth(force_refresh=True)
            if not token or not account_id:
                raise
            payload = await asyncio.to_thread(
                self._fetch_blocking, key, account_id, sandbox, token,
            )
        self._cache[key] = (time.monotonic(), payload)
        return payload

    def invalidate(self, game_id: str) -> None:
        """Drop a game's cached achievements."""
        self._cache.pop(str(game_id), None)


    def _resolve_sandbox(self, game_id: str) -> str | None:
        """The game's sandboxId = legendary metadata ``namespace`` (cache file)."""
        meta_file = self._user_file.parent / "metadata" / f"{game_id}.json"
        try:
            if meta_file.is_file():
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                ns = (data.get("metadata") or {}).get("namespace")
                return str(ns) if ns else None
        except (OSError, ValueError):
            logger.debug("[epic.achievements] metadata read failed", exc_info=True)
        return None

    # -- fetch + merge (worker thread) -------------------------------------

    def _fetch_blocking(
        self, game_id: str, account_id: str, sandbox: str, token: str,
    ) -> dict[str, Any]:
        """Defs + state GraphQL, merged by achievement name → normalized payload."""
        defs = self._graphql(token, _DEFS_QUERY, {"sandboxId": sandbox, "locale": "en"})
        state = self._graphql(
            token, _STATE_QUERY,
            {"epicAccountId": account_id, "sandboxId": sandbox},
        )
        def_rec = (
            (defs.get("Achievement") or {})
            .get("productAchievementsRecordBySandbox") or {}
        )
        definitions = def_rec.get("achievements") or []
        state_rec = self._first(
            ((state.get("PlayerAchievement") or {})
             .get("playerAchievementGameRecordsBySandbox") or {}).get("records"),
        )
        unlocked_state = {
            pa["playerAchievement"]["achievementName"]: pa["playerAchievement"]
            for pa in (state_rec.get("playerAchievements") or [])
            if isinstance(pa, dict) and pa.get("playerAchievement")
        }
        return self._build_payload(game_id, definitions, unlocked_state)

    def _graphql(
        self, token: str, query: str, variables: dict[str, Any],
    ) -> dict[str, Any]:
        """POST a GraphQL op; return ``data`` (or raise a typed error).

        Epic's storefront GraphQL is behind Cloudflare bot protection that 403s
        the TLS fingerprint of the Decky plugin runtime — its in-process Python
        loads the Steam Runtime's old libssl (via ``LD_LIBRARY_PATH``). A
        ``curl`` subprocess with that env scrubbed uses the system TLS stack and
        IS accepted, so we shell out for this call; plain ``urllib`` is a
        fallback for environments without curl / without the pollution.
        """
        payload = json.dumps({"query": query, "variables": variables}).encode()
        curl_result = self._post_via_curl(token, payload)
        if curl_result is None:
            parsed = self._post_via_urllib(token, payload)  # curl absent
        else:
            code, raw = curl_result
            if code == 401:
                raise EpicAchievementsError("auth_expired")
            if code != 200:
                logger.warning("[epic.achievements] GraphQL HTTP %s (curl)", code)
                raise EpicAchievementsError("offline")
            try:
                parsed = json.loads(raw)
            except ValueError as e:
                raise EpicAchievementsError("offline") from e
        # A GraphQL error with no data → surface as offline/unavailable.
        data = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(data, dict):
            raise EpicAchievementsError("offline")
        return data

    def _post_via_curl(
        self, token: str, payload: bytes,
    ) -> tuple[int, str] | None:
        """POST via curl with a scrubbed env (system TLS). None if curl absent.

        Returns ``(http_code, body)``. ``LD_LIBRARY_PATH``/``LD_PRELOAD`` are
        stripped so curl links the system libssl rather than the Steam Runtime's
        — that's the whole point (Cloudflare accepts the system TLS fingerprint).
        """
        curl = shutil.which("curl")
        if not curl:
            return None
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD")
        }
        try:
            proc = subprocess.run(
                [
                    curl, "-sS", "--max-time", "15", "-X", "POST", _GRAPHQL_URL,
                    "-H", f"Authorization: bearer {token}",
                    "-H", "Content-Type: application/json",
                    "-H", f"User-Agent: {_UA}",
                    "--data-binary", "@-", "-w", "\n%{http_code}",
                ],
                input=payload, capture_output=True, env=env,
                timeout=20, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise EpicAchievementsError("offline") from e
        out = proc.stdout.decode(errors="ignore")
        nl = out.rfind("\n")
        if nl < 0:
            raise EpicAchievementsError("offline")
        try:
            code = int(out[nl + 1:].strip())
        except ValueError:
            code = 0
        return code, out[:nl]

    def _post_via_urllib(self, token: str, payload: bytes) -> Any:
        """Fallback POST via urllib (dev shells / unpolluted environments)."""
        ctx = ssl_ctx_permissive("Epic GraphQL — outdated Deck cert store")
        req = urllib.request.Request(
            _GRAPHQL_URL, data=payload, method="POST", headers={
                "Authorization": f"bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": _UA,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            logger.warning("[epic.achievements] GraphQL HTTP %s (urllib)", e.code)
            if e.code == 401:
                raise EpicAchievementsError("auth_expired") from e
            raise EpicAchievementsError("offline") from e
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
            raise EpicAchievementsError("offline") from e

    @staticmethod
    def _first(value: Any) -> dict[str, Any]:
        """GOG-style ``records`` is a list (or null); take the first record."""
        if isinstance(value, list) and value:
            return value[0] if isinstance(value[0], dict) else {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _build_payload(
        game_id: str,
        definitions: list[dict[str, Any]],
        unlocked_state: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Merge defs + state into the normalized achievements payload."""
        normalized = [
            EpicAchievements._normalize(d["achievement"], unlocked_state)
            for d in definitions
            if isinstance(d, dict) and isinstance(d.get("achievement"), dict)
        ]
        unlocked = [a for a in normalized if a["unlocked"]]
        locked = [a for a in normalized if not a["unlocked"]]
        unlocked.sort(key=lambda a: a["unlocked_at"] or 0.0, reverse=True)
        ordered = unlocked + locked
        total = len(ordered)
        n_unlocked = len(unlocked)
        return {
            "store": "epic",
            "game_id": str(game_id),
            "total": total,
            "unlocked": n_unlocked,
            "percent": round(n_unlocked / total * 100, 1) if total else 0.0,
            "achievements": ordered,
        }

    @staticmethod
    def _normalize(
        defn: dict[str, Any], unlocked_state: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """One Epic achievement (definition + state) → the frontend shape."""
        key = str(defn.get("name") or "")
        st = unlocked_state.get(key, {})
        unlocked = bool(st.get("unlocked"))
        unlocked_icon = str(defn.get("unlockedIconLink") or "")
        desc = defn.get("unlockedDescription") if unlocked else (
            defn.get("lockedDescription") or defn.get("unlockedDescription")
        )
        return {
            "key": key,
            "name": str(
                defn.get("unlockedDisplayName")
                or defn.get("lockedDisplayName")
                or key,
            ),
            "description": str(desc or ""),
            "image_unlocked": unlocked_icon,
            "image_locked": str(defn.get("lockedIconLink") or unlocked_icon),
            "hidden": bool(defn.get("hidden")),
            "unlocked": unlocked,
            "unlocked_at": parse_timestamp(st.get("unlockDate")),
            "rarity": (defn.get("rarity") or {}).get("percent"),
        }
