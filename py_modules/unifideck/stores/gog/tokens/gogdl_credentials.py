"""Temporary gogdl credentials directory — used by subprocess calls.

``gogdl`` (the CLI used by the installer pipeline) reads tokens from
its own config directory, in clear text. We don't want to point gogdl
at our encrypted store, and we don't want to leave plaintext credentials
on disk permanently.

``_GogdlCreds.acquire`` creates a unique tmpdir, writes
``gog_credentials.json`` (with ``mode=0o600``) holding the current
tokens, and returns:

* an ``env`` dict whose ``GOGDL_CONFIG_PATH`` points at the **persistent
  parent of** ``gogdl_config_dir`` (typically ``~/.config/unifideck``).
  gogdl needs that location to be persistent so it can populate the
  ``heroic_gogdl/manifests/`` cache and the dependencies repository
  between runs — pointing it at the credentials tmpdir caused
  installs to hang at ``[API] Getting Dependencies repository``.
* the ``creds_path`` of the just-written ``gog_credentials.json`` —
  callers pass this verbatim to gogdl's ``--auth-config-path`` flag so
  the auth file location stays in sync with where credentials were
  actually written.
* a cleanup coroutine that wipes the tmpdir.

Used by ``install/progress.py`` and ``install/marker.py``
 when they spawn gogdl subprocesses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.core.binaries import clean_cli_env

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from unifideck.stores.gog.config import GOGConfig

    CleanupFn = Callable[[], Awaitable[None]]
logger = logging.getLogger(__name__)

class _GogdlCreds:
    """Gogdl creds."""

    def __init__(self, *, config: GOGConfig) -> None:
        """Initialize the instance."""
        self._config = config

    async def acquire(
        self,
        access_token: str,
        refresh_token: str,
    ) -> tuple[dict[str, str], str, CleanupFn]:
        """Acquire."""
        tmpdir = await asyncio.to_thread(
            tempfile.mkdtemp,
            "unifideck-gogdl-",
        )
        creds_path = str(Path(tmpdir) / "gog_credentials.json")
        gogdl_data = self._build_gogdl_data(
            access_token,
            refresh_token,
        )
        await asyncio.to_thread(
            self._write_creds_sync,
            creds_path,
            gogdl_data,
        )
        # Scrubbed rather than a raw os.environ copy: this env is handed to
        # every gogdl invocation, and gogdl >=1.2.2 is a zipapp running under
        # the system python3 — so the frozen Decky loader's
        # LD_LIBRARY_PATH=/tmp/_MEIxxxx and any stray PYTHONPATH now reach an
        # interpreter that actually obeys them.
        env = clean_cli_env()
        # GOGDL_CONFIG_PATH must be the persistent parent of
        # ``gogdl_config_dir`` so gogdl can populate / reuse its
        # ``heroic_gogdl/manifests/`` and dependencies-repo cache between
        # runs. Pointing it at the credentials tmpdir caused installs
        # to hang at "[API] Getting Dependencies repository".
        # ``expanduser`` here is a cheap ``~`` → ``$HOME`` substitution,
        # not blocking filesystem I/O, so it's safe in this async path.
        env["GOGDL_CONFIG_PATH"] = str(
            Path(self._config.gogdl_config_dir).expanduser().parent,  # noqa: ASYNC240
        )
        # CRITICAL: Force unbuffered Python output in gogdl.
        # Without this, gogdl (a Python script) buffers output when
        # stdout is piped, causing the asyncio output reading loop to
        # hang/timeout and downloads to fail.
        env["PYTHONUNBUFFERED"] = "1"
        cleanup = self._make_cleanup(creds_path, tmpdir)
        return env, creds_path, cleanup

    def _build_gogdl_data(
        self,
        access_token: str,
        refresh_token: str,
    ) -> dict[str, dict[str, object]]:
        """Build GOGDL data."""
        now = time.time()
        return {
            self._config.client_id: {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid",
                "created_at": now,
                "loginTime": now,
            },
        }

    @staticmethod
    def _write_creds_sync(
        creds_path: str,
        gogdl_data: dict[str, dict[str, object]],
    ) -> None:
        """Write creds sync."""
        fd = os.open(
            creds_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(gogdl_data, f)

    @staticmethod
    def _make_cleanup(creds_path: str, tmpdir: str) -> CleanupFn:
        """Make cleanup."""

        async def _cleanup() -> None:
            """Cleanup."""

            def _remove() -> None:
                """Remove."""
                try:
                    if Path(creds_path).is_file():
                        Path(creds_path).unlink()
                    if Path(tmpdir).is_dir():
                        Path(tmpdir).rmdir()
                except OSError as e:
                    logger.warning(
                        "[GOGTokens] gogdl temp cleanup failed: %s",
                        e,
                    )

            await asyncio.to_thread(_remove)

        return _cleanup
