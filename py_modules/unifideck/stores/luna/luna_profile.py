"""Luna browser-game profile directory.

The Luna kiosk runs the plugin's own downloaded Chromium, a native binary,
so it can write to any directory the plugin user owns and no sandbox
filesystem grant is involved. This module only resolves, creates and checks
that profile directory:

- :func:`resolve_profile_dir` — PURE. Defaults to ``WEB_GAME_PROFILE_DIR``
  only when ``luna.profile_dir`` is unset (Req 12.8) and returns an
  absolute, ``~``-expanded path.

- :class:`LunaProfile` — creates the directory if absent (Req 8.1), checks
  writability (Req 1.9) and confirms it still exists once the browser has
  started (Req 8.4).

Errors are surfaced as :class:`LunaProfileError` naming the offending path:
a ``profile_permission`` error when the dir is not writable (and the browser
is *not* opened, Req 1.9), and a ``profile_not_persisted`` error when the dir
is absent after the browser started (Req 8.4).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from unifideck.auth.edge_browser.edge import WEB_GAME_PROFILE_DIR

logger = logging.getLogger(__name__)


class LunaProfileError(Exception):
    """A Luna profile failure that names the offending path.

    ``kind`` is one of ``"profile_permission"`` (dir not writable at
    connect — do not open the browser, Req 1.9) or
    ``"profile_not_persisted"`` (dir absent after the browser started,
    Req 8.4).
    """

    def __init__(self, kind: str, path: str, message: str) -> None:
        """Build the error carrying its kind and the named path."""
        super().__init__(message)
        self.kind = kind
        self.path = path


def resolve_profile_dir(configured: str | None) -> str:
    """The Luna profile dir, defaulting to ``WEB_GAME_PROFILE_DIR``.

    The shared browser-game profile is used only when ``luna.profile_dir``
    is unset (Req 12.8); any configured value wins. The result is an
    absolute, ``~``-expanded path with no trailing separator.
    """
    raw = configured if configured else WEB_GAME_PROFILE_DIR
    return os.path.normpath(str(Path(raw).expanduser()))


@dataclass(frozen=True)
class LunaProfile:
    """The Luna profile directory and its create/writable/persisted checks.

    Constructed from the resolved profile dir so the launcher subprocess and
    the plugin backend can both build it from ``LunaConfig`` alone.
    """

    profile_dir: str

    @classmethod
    def from_config(cls, configured_profile_dir: str | None) -> LunaProfile:
        """Build from the configured ``luna.profile_dir``."""
        return cls(profile_dir=resolve_profile_dir(configured_profile_dir))

    def ensure_profile_dir(self) -> str:
        """Create the Luna profile dir if absent and confirm it is writable.

        Returns the resolved profile dir on success. Raises
        :class:`LunaProfileError` (``profile_permission``) naming the path
        when the dir cannot be created or is not writable, so the caller
        does **not** open the browser (Req 1.9, 8.1).
        """
        path = Path(self.profile_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = f"Luna profile dir is not writable: {self.profile_dir} ({exc})"
            logger.warning("[LunaProfile] %s", msg)
            raise LunaProfileError(
                "profile_permission", self.profile_dir, msg,
            ) from exc
        if not os.access(self.profile_dir, os.W_OK):
            msg = f"Luna profile dir is not writable: {self.profile_dir}"
            logger.warning("[LunaProfile] %s", msg)
            raise LunaProfileError(
                "profile_permission", self.profile_dir, msg,
            )
        return self.profile_dir

    def verify_persisted(self) -> None:
        """Confirm the profile dir exists after the browser started.

        Raises :class:`LunaProfileError` (``profile_not_persisted``) when the
        dir is absent, because a session signed in without a persisted
        profile would be lost on the next launch (Req 8.4).
        """
        if not Path(self.profile_dir).is_dir():
            msg = f"Luna profile dir absent after browser start: {self.profile_dir}"
            logger.warning("[LunaProfile] %s", msg)
            raise LunaProfileError(
                "profile_not_persisted", self.profile_dir, msg,
            )
