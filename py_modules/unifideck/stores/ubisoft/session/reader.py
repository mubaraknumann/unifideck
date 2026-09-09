"""
Session reader — extract auth state from a Wine prefix.

``_CredentialReader`` reads UPC's authenticated state out of a Wine prefix:

* whether the vault is present and plausible (:meth:`has_valid_credentials`);
* whether it is actually SIGNED IN (:meth:`is_signed_in`);
* how fresh it is (:meth:`get_credential_mtime`);
* which prefix is the best source to copy from
  (:meth:`find_best_credential_source`).

The reader is read-only — propagation happens through ``payload.py``.
The split between reader and payload exists so the same parsed session
can be propagated to multiple target prefixes without re-reading.

**Freshness is mtime, never size.** Ubisoft rotates the refresh token on
every sign-in, and a rotated vault is routinely a few hundred bytes
smaller than the one before it. Ranking by size froze the auth prefix on
the first token it ever saw (GH #435).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .payload import _CSS_MIN_SOURCE_SIZE

if TYPE_CHECKING:
    from unifideck.stores.ubisoft.config import UbisoftConfig
    from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths
_CSS_MIN_VALID_SIZE = 100

#: The entry UPC writes into ``ConnectSecureStorage.dat`` when an account is
#: attached, and strips on sign-out. Measured on-device 2026-09-05 by diffing
#: a signed-in vault against the one UPC wrote after it signed itself out:
#:
#:     $ diff <(strings signed_out.dat) <(strings signed_in.dat)
#:     > RememberMeTicket
#:     > oJlG/GsqFhfOkeuJXF8MQWsGqOKrZZYU5GRadANQo+8FHE51OcDxq22GXtJhds7G…
#:
#: That key and its base64 blob are the ONLY difference; the signed-out vault
#: has nothing unique of its own. Verified across all five vaults on that
#: device (auth, template, two game prefixes, a backup): every signed-in vault
#: carries it, neither signed-out vault does. Plain ASCII in the file, not
#: UTF-16, so a byte scan is enough and no parser is needed.
_SIGNED_IN_MARKER = b"RememberMeTicket"

logger = logging.getLogger(__name__)

class _CredentialReader:
    """Credential reader."""

    def __init__(
        self,
        *,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._paths = paths

    def has_valid_credentials(self, prefix_path: str) -> bool:
        """Check whether valid credentials."""
        for _root, user_home in self._paths.iter_user_homes(
            prefix_path,
            pfx_first=True,
        ):
            css = self._css_path(user_home)
            if self._is_valid_css(css, _CSS_MIN_VALID_SIZE):
                return True
        return False

    def is_signed_in(self, prefix_path: str) -> bool:
        """True when this prefix holds a SIGNED-IN UPC session.

        The discriminator is the vault's *contents*: a plausible
        ``ConnectSecureStorage.dat`` that still carries
        :data:`_SIGNED_IN_MARKER`.

        It is deliberately neither of the two rules that came before:

        *Not size.* "Smaller vault than the auth prefix means logged out" was
        a monotonic ratchet — Ubisoft rotates the refresh token on every
        sign-in and a rotated vault is routinely a few hundred bytes smaller,
        so the first rotation froze the auth prefix on a server-dead token
        (GH #435, reproduced).

        *Not the presence of ``user.dat``.* That replaced the size rule and
        was inert from the day it landed. It assumed the pristine
        ``.template`` carries no ``user.dat`` and that sign-out removes it —
        both false on a real device. Measured 2026-09-05: ``user.dat`` is
        byte-identical (``900aac14…``, 516 bytes) in the template, the auth
        prefix and every cloned game prefix, and UPC's own sign-out left it
        untouched while rewriting the vault. So the check could never return
        False for a cloned prefix, and the capture guard it backs never fired
        once. Observed live: a game prefix that UPC had signed itself out of
        was captured into ``.upc-auth`` and then fanned out over every other
        prefix, destroying a good token that had not yet been captured back.
        """
        for _root, user_home in self._paths.iter_user_homes(
            prefix_path,
            pfx_first=True,
        ):
            css = self._css_path(user_home)
            if not self._is_valid_css(css, _CSS_MIN_VALID_SIZE):
                continue
            if self._vault_has_account(css):
                return True
        return False

    @staticmethod
    def _vault_has_account(css: str) -> bool:
        """Whether ``css`` still carries the signed-in marker.

        Unreadable is treated as "no account": the guard this feeds protects
        the auth prefix, and refusing to capture from a vault we cannot read
        is the safe direction — the next play captures instead.
        """
        try:
            return _SIGNED_IN_MARKER in Path(css).read_bytes()
        except OSError as e:
            logger.warning(
                "[UbisoftSession] cannot read vault %s: %s", css, e,
            )
            return False

    def get_credential_mtime(self, prefix_path: str) -> float:
        """Get credential mtime."""
        best: float = 0.0
        for _root, user_home in self._paths.iter_user_homes(
            prefix_path,
            pfx_first=True,
        ):
            css = self._css_path(user_home)
            if not self._is_valid_css(css, _CSS_MIN_VALID_SIZE):
                continue
            try:
                mtime = Path(css).stat().st_mtime
            except OSError:
                continue
            if mtime > best:
                best = mtime
        return best

    def find_best_credential_source(self) -> str | None:
        """Find best credential source."""
        auth_source = self._check_auth_prefix_for_credentials()
        if auth_source:
            return auth_source
        return self._find_freshest_game_prefix_credentials()

    def _check_auth_prefix_for_credentials(self) -> str | None:
        """Check auth prefix for credentials."""
        auth_dir = self._config.auth_prefix_dir_expanded
        if not Path(auth_dir).is_dir():
            return None
        for _root, user_home in self._paths.iter_user_homes(
            auth_dir,
            pfx_first=True,
        ):
            css = self._css_path(user_home)
            if self._is_valid_css(css, _CSS_MIN_SOURCE_SIZE):
                return auth_dir
        return None

    def _find_freshest_game_prefix_credentials(
        self,
    ) -> str | None:
        """Find freshest game prefix credentials."""
        prefixes_dir = self._config.prefixes_dir_expanded
        prefixes_p = Path(prefixes_dir)
        if not prefixes_p.is_dir():
            return None
        try:
            entries = list(prefixes_p.iterdir())
        except OSError:
            return None
        best_mtime: float = 0.0
        best_prefix: str | None = None
        for entry in entries:
            if not entry.is_dir():
                continue
            # Dot-entries are infrastructure, not game prefixes: ``.template``
            # is pristine by invariant, and ``logout()`` leaves the signed-out
            # auth prefix behind as ``.upc-auth.trash-<ms>`` until its
            # background rmtree lands. Picking either up as a fallback source
            # re-seeds a session the user just discarded. ``config
            # .iter_game_prefix_paths`` already applies this filter; this scan
            # walks ``prefixes_dir`` directly and did not.
            if entry.name.startswith("."):
                continue
            prefix = str(entry)
            mtime = self._best_css_mtime_for_prefix(prefix)
            if mtime is not None and mtime > best_mtime:
                best_mtime = mtime
                best_prefix = prefix
        return best_prefix

    def _best_css_mtime_for_prefix(
        self,
        prefix: str,
    ) -> float | None:
        """Best CSS mtime for prefix."""
        for _root, user_home in self._paths.iter_user_homes(
            prefix,
            pfx_first=True,
        ):
            css = self._css_path(user_home)
            if not self._is_valid_css(css, _CSS_MIN_SOURCE_SIZE):
                continue
            try:
                return Path(css).stat().st_mtime
            except OSError:
                continue
        return None

    def _css_path(self, user_home: str) -> str:
        """Css path."""
        return str(
            Path(user_home) / self._config.upc_local_subdir / "ConnectSecureStorage.dat"
        )

    @staticmethod
    def _is_valid_css(css_path: str, min_size: int) -> bool:
        """Is valid CSS."""
        css_p = Path(css_path)
        if not css_p.is_file():
            return False
        try:
            return css_p.stat().st_size > min_size
        except OSError:
            return False
