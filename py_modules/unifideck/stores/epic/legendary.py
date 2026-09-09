"""Thin wrapper for the ``legendary`` CLI — info-fetch helper.

A single module-level function (no class) that wraps the
``legendary info <game_id>`` subprocess call and returns the parsed
JSON output. Used by ``install.py``, ``library.py``, and
``updates.py`` whenever they need game metadata.

``fetch_info(game_id, legendary_bin)`` :

* spawns ``legendary info --json <game_id>``;
* parses stdout as JSON;
* returns the parsed dict (or None on failure);
* swallows subprocess errors and logs them — the caller decides
  how to react to a missing info response.

``legendary_config_dir()`` resolves where legendary keeps its state,
for the several callers that read those files directly instead of
paying for a subprocess.

Kept as a small module because the surface is trivial and extracting
it elsewhere would just spread the legendary-CLI coupling across more
files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from configparser import (
    ConfigParser,
    DuplicateSectionError,
    MissingSectionHeaderError,
)
from pathlib import Path
from typing import Any, cast

from unifideck.core.binaries import clean_cli_env
from unifideck.utils.lang_normalize import normalize_language

_logger = logging.getLogger(__name__)

def legendary_config_dir() -> Path:
    """Return legendary's config dir (honours ``LEGENDARY_CONFIG_DIR``).

    legendary itself reads this variable, so a user (or a test) can point
    it at a non-default directory and every path we derive must follow —
    hence never hardcoding the default.

    This docstring used to credit ``security.ephemeral_creds`` with
    setting the variable. Nothing ever did: that module had no callers in
    the project's history and was deleted (audit §1.4 f). The credential
    file it lives beside is protected by
    :func:`unifideck.stores.shared.cli_credentials.harden_cli_credential_file`
    instead, which is mode-only — not encryption at rest.
    """
    env = os.environ.get("LEGENDARY_CONFIG_DIR")
    return (
        Path(env).expanduser() if env
        else Path("~/.config/legendary").expanduser()
    )

def write_app_language(app_name: str, language: str) -> None:
    """Record a per-game UI language in legendary's ``config.ini``.

    legendary resolves a launch's ``-epiclocale`` as
    ``config.get(app_name, 'language', fallback=<--language arg>)``, so
    writing this key makes the language a user picked *for this game* at
    install time outrank the global Unifideck language the launcher
    passes — and it keeps working if the game is ever launched through
    legendary directly.

    legendary only writes this key itself under ``--set-defaults``, which
    would also persist our umu wrapper and ``no_wine`` into the user's
    config, so we set just this one key ourselves.

    ``language`` is normalized to the ISO base code legendary documents
    for ``--language``. Best-effort: a failure only means the launcher
    falls back to the global language.
    """
    code = normalize_language(language) or ""
    if not app_name or not code:
        return
    path = legendary_config_dir() / "config.ini"
    # Mirror legendary's own parser (``LGDConf`` in lfs/lgndry.py):
    # ``comment_prefixes='/'`` means its ``;`` help comments parse as
    # valueless keys and survive the rewrite instead of being stripped.
    # ``optionxform`` is ours — it keeps their original casing too.
    parser = ConfigParser(comment_prefixes="/", allow_no_value=True)
    parser.optionxform = str  # type: ignore[method-assign,assignment]
    try:
        if path.exists():
            parser.read(path)
        if not parser.has_section(app_name):
            parser.add_section(app_name)
        parser.set(app_name, "language", code)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            parser.write(f)
    except (OSError, DuplicateSectionError, MissingSectionHeaderError) as e:
        _logger.warning(
            "[epic_legendary] could not record language for %s: %s",
            app_name, e,
        )
        return
    _logger.info(
        "[epic_legendary] recorded language=%s for %s", code, app_name,
    )

async def fetch_info(cli_path: str, game_id: str, *, timeout: float, log_prefix: str = "[epic_legendary]") -> dict[str, Any] | None:  # noqa: ASYNC109 — timeout is API value passed to underlying lib (urllib/aiohttp/subprocess), not an asyncio.timeout() wrapper
    """Fetch info."""
    if not cli_path:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            cli_path,
            "info",
            game_id,
            "--json",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=clean_cli_env(),
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except (TimeoutError, OSError) as e:
        _logger.warning("%s legendary info failed: %s", log_prefix, e)
        return None
    if proc.returncode != 0:
        return None
    try:
        return cast("dict[str, Any] | None", json.loads(stdout.decode(errors="ignore")))
    except json.JSONDecodeError as e:
        _logger.warning(
            "%s JSON parse error: %s",
            log_prefix,
            e,
        )
        return None
