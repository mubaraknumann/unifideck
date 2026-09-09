"""Install-pipeline helpers — game info probe + language picking.

``_InstallHelpers`` exposes the two helper methods that the installer
calls during the "probe & prepare" phase :

* ``probe_game_info(game_id)`` — query gogdl for the game's platform,
  expected folder name, and supported languages;
* ``pick_languages(preferred, explicit, supported)`` — given the user's
  locale and the game's available languages, decide which language
  list to pass to gogdl. Honors an explicit override (always wins) or
  picks a smart match (delegates to ``languages.py``).

Refactor history (2026-05-14): ``parse_info_output`` was a single
function at CC=17 — the reversed-line walk inlined the JSON parse,
the skip-empty + skip-malformed branches, and the two-field
extraction (folder_name + languages) with their dedicated isinstance
guard. Cognitive complexity exploded because the nesting reached
4 levels deep on the ``languages`` branch. Split into a small
generator (``_iter_json_lines_reversed``) + two pure extractors so
the main loop is a flat assignment-and-break read.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Iterator
from typing import (
    TYPE_CHECKING,
    Any,
)

from .languages import smart_match_language

if TYPE_CHECKING:
    from .installer import GOGInstaller
logger = logging.getLogger(__name__)

class _InstallHelpers:
    """Install helpers."""

    def __init__(self, parent: GOGInstaller) -> None:
        """Initialize the instance."""
        self._parent = parent

    async def probe_game_info(self, game_id: str) -> tuple[str, str | None, list[str]]:
        """Probe game info."""
        platform = "linux"
        folder_name: str | None = None
        languages: list[str] = []
        for trial_platform in ("linux", "windows"):
            env, creds_path, _gogdl_cleanup = await self._parent._tokens.acquire_gogdl_creds()
            cmd = [
                self._parent._gogdl_bin,
                "--auth-config-path",
                creds_path,
                "info",
                "--platform",
                trial_platform,
                game_id,
            ]
            stdout = b""
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                try:
                    stdout, _stderr = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=60,
                    )
                except TimeoutError:
                    logger.warning(
                        "[GOGInstaller] gogdl info timed out on "
                        "%s/%s — killing subprocess",
                        trial_platform,
                        game_id,
                    )
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
                    stdout = b""
            finally:
                await _gogdl_cleanup()
            if proc.returncode != 0 and trial_platform == "linux":
                logger.info(
                    "[GOGInstaller] no Linux build for %s, trying Windows",
                    game_id,
                )
                continue
            platform = trial_platform
            folder_name, languages = self.parse_info_output(
                stdout.decode(errors="replace"),
            )
            break
        if folder_name:
            logger.info(
                "[GOGInstaller] info: platform=%s folder=%s langs=%s",
                platform,
                folder_name,
                languages,
            )
        return platform, folder_name, languages

    @staticmethod
    def parse_info_output(stdout: str) -> tuple[str | None, list[str]]:
        """Parse ``gogdl info`` stdout and return ``(folder_name, languages)``.

        gogdl emits one JSON object per line ; the interesting
        fields (``folder_name`` and ``languages``) usually live on
        the last few lines, so we iterate bottom-up and short-
        circuit as soon as both are filled. Returns ``(None, [])``
        when the output contains no usable JSON line — caller
        decides how to react (typically fall back to defaults).
        """
        folder_name: str | None = None
        languages: list[str] = []
        for data in _InstallHelpers._iter_json_lines_reversed(stdout):
            if folder_name is None:
                folder_name = _InstallHelpers._extract_folder_name(data)
            if not languages:
                languages = _InstallHelpers._extract_languages(data)
            if folder_name and languages:
                break
        return folder_name, languages

    # ─────────────────────────────────────────────────────────────
    # Helpers extracted from the former CC=17 parse_info_output
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _iter_json_lines_reversed(stdout: str) -> Iterator[dict[str, Any]]:
        """Yield parsed JSON objects from ``stdout``, bottom-up.

        Skips empty lines and lines that fail to parse — gogdl
        occasionally emits human-readable log lines interleaved
        with the structured output, and we don't want those to
        abort the walk.
        """
        for raw_line in reversed(stdout.splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    @staticmethod
    def _extract_folder_name(data: dict[str, Any]) -> str | None:
        """Return the ``folder_name`` field from a gogdl JSON line, or None.

        Defensive against type coercions: gogdl has occasionally
        emitted nulls or numeric values here ; we only accept
        non-empty strings.
        """
        val = data.get("folder_name")
        if isinstance(val, str) and val:
            return val
        return None

    @staticmethod
    def _extract_languages(data: dict[str, Any]) -> list[str]:
        """Return the ``languages`` field as a list of str, or empty.

        The wire format guarantees a JSON array of locale tags
        (``["en-US", "fr-FR", …]``) but past versions of gogdl
        have emitted unexpected shapes (single string, null) on
        edge games — we coerce to ``list[str]`` and silently
        return ``[]`` on anything else.
        """
        langs = data.get("languages")
        if isinstance(langs, list):
            return [str(x) for x in langs]
        return []

    @staticmethod
    def pick_languages(
        primary_lang: str,
        explicit: bool,
        supported: list[str],
    ) -> list[str]:
        """Pick languages."""
        if explicit:
            return _InstallHelpers._pick_explicit_lang(primary_lang)
        return _InstallHelpers._pick_implicit_langs(
            primary_lang,
            supported,
        )

    @staticmethod
    def _pick_explicit_lang(primary_lang: str) -> list[str]:
        """Pass the user's explicitly-picked language to gogdl VERBATIM.

        The value was selected from the game's own gogdl language list
        (the install modal shows exactly what gogdl reported), so it is
        already a valid ``--lang`` code for this title. We must NOT
        remap it — remapping silently changed picks like ``es-MX`` into
        a different variant or fell back to English. Any normalization
        is for the picker's display only, never the code we send.
        """
        return [primary_lang]

    @staticmethod
    def _pick_implicit_langs(primary_lang: str, supported: list[str]) -> list[str]:
        """Pick implicit langs."""
        if not supported:
            langs = [primary_lang]
            if "en-US" not in langs:
                langs.append("en-US")
            return langs
        result: list[str] = []
        matched = smart_match_language(primary_lang, supported)
        if matched:
            result.append(matched)
        else:
            matched_english = smart_match_language(
                "en-US",
                supported,
            )
            if matched_english:
                result.append(matched_english)
            else:
                result.append(supported[0])
        return result
