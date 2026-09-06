"""
UPC session facade — propagate auth state across Wine prefixes.

UPC stores its auth state (credentials, refresh tokens, machine GUID)
inside the Wine prefix where the user signed in. To launch games from
other prefixes we have to copy that state into each prefix on demand.

``UbisoftSession`` is the orchestration class for this propagation. It
delegates to:

* ``reader.py`` — read sessions out of the auth prefix;
* ``payload.py`` — copy credentials/artifacts to target prefixes;
* ``propagator.py`` — orchestrate propagation across multiple
  game prefixes when the auth state changes.

* ``lock.py`` — serialise all of that against the launcher process.

The session facade exposes the ``_read_machine_guid`` helper used by the
payload module to refuse copying credentials into a prefix of a different
identity. That guard is conservative, not cryptographic: Wine's DPAPI does
not key on the machine GUID (see ``payload.py``'s note), so a GUID match is
never the reason a session does or does not work.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from unifideck.stores.ubisoft.config import UbisoftConfig
from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths

from .lock import session_lock
from .payload import _PayloadSync
from .propagator import _CredentialPropagator
from .reader import _CredentialReader

logger = logging.getLogger(__name__)
_CAPTURE_SENTINEL = "credentials_captured"

class UbisoftSession:
    """Ubisoft session."""

    def __init__(
        self,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
        read_machine_guid: Callable[[str], str],
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._paths = paths
        self._read_machine_guid = read_machine_guid
        self._reader = _CredentialReader(
            config=config,
            paths=paths,
        )
        self._payload = _PayloadSync(self)
        self._propagator = _CredentialPropagator(
            config=config,
            paths=paths,
            payload=self._payload,
            reader=self._reader,
        )

    def has_valid_credentials(self, prefix_path: str) -> bool:
        """Whether a plausible vault EXISTS in this prefix.

        Presence, not state: a signed-OUT vault satisfies this. Use it to
        answer "is there something here to copy / inject", never "is the
        user signed in" — :meth:`is_signed_in` answers that one.
        """
        return self._reader.has_valid_credentials(prefix_path)

    def is_signed_in(self, prefix_path: str) -> bool:
        """Whether this prefix holds a vault with an account attached."""
        return self._reader.is_signed_in(prefix_path)

    def get_credential_mtime(self, prefix_path: str) -> float:
        """Get credential mtime."""
        return self._reader.get_credential_mtime(prefix_path)

    def find_best_credential_source(self) -> str | None:
        """Find best credential source."""
        return self._reader.find_best_credential_source()

    def _is_valid_css(self, css_path: str, min_size: int) -> bool:
        """Is valid CSS."""
        return self._reader._is_valid_css(css_path, min_size)

    def propagate_credentials_to_all(self) -> int:
        """Propagate credentials to all."""
        return self._propagator.propagate_credentials_to_all()

    def propagate_auth_artifacts_to_all(self) -> int:
        """Propagate auth artifacts to all."""
        return self._propagator.propagate_auth_artifacts_to_all()

    def propagate_all_to_all(self) -> None:
        """Propagate all to all."""
        with self._serialised():
            self._propagator.propagate_all_to_all()

    def purge_credentials_from_all(self) -> int:
        """Purge credentials from all game prefixes + the template."""
        with self._serialised():
            return self._propagator.purge_credentials_from_all()

    def inject_into_prefix(self, prefix_path: str) -> bool:
        """Inject into prefix."""
        with self._serialised():
            return self._propagator.inject_into_prefix(prefix_path)

    def _serialised(self) -> AbstractContextManager[bool]:
        """The cross-process session lock, over ``data_dir``.

        Held around every operation that REWRITES credential files, so a
        backend fan-out cannot interleave with the launcher seeding or
        capturing a prefix. Read-only helpers are deliberately outside it.
        """
        return session_lock(self._config.data_dir_expanded)

    def ensure_auth_state_in_prefixes(
        self,
        prefix_paths: list[str],
    ) -> int:
        """Seed ``prefix_paths`` from the auth prefix, refreshing it first.

        The refresh is the durability half of post-play capture. Capture runs
        on ``GAME_STOPPED`` and can legitimately not run at all — the bus
        watchdog cancels it, the backend restarts between the game exiting and
        the handler finishing, the user force-quits. When it does not run, the
        rotated refresh token stays in the game prefix and ``.upc-auth`` keeps
        a token the server has already retired. Nothing notices until the next
        fresh install seeds that dead token into a new prefix and UPC opens on
        the login screen, which is a confusing distance from the cause.

        So before seeding, promote the freshest signed-in vault on the device
        into the auth prefix. Measured 2026-09-05: two consecutive captures
        were cancelled at ~5s, the game prefix held a vault 2m26s newer than
        the auth prefix, and the install nine minutes later signed the user
        out. This closes that window whatever caused the capture to be missed.
        """
        with self._serialised():
            self._refresh_auth_from_freshest_prefix()
        return self._propagator.ensure_auth_state_in_prefixes(
            prefix_paths,
        )

    def _refresh_auth_from_freshest_prefix(self) -> None:
        """Capture into auth from the newest signed-in game prefix, if any.

        Only ever moves auth FORWARD: a candidate must be signed in and
        strictly newer than what auth already holds. ``_capture_locked``
        re-checks both, so this picks the candidate and lets the one guarded
        path do the writing rather than opening a second one.
        """
        auth_dir = self._config.auth_prefix_dir_expanded
        auth_mtime = self._reader.get_credential_mtime(auth_dir)
        best: tuple[float, str] | None = None
        for prefix_path in self._paths.iter_all_game_prefix_paths():
            if not self._reader.is_signed_in(prefix_path):
                continue
            mtime = self._reader.get_credential_mtime(prefix_path)
            if mtime <= auth_mtime:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, prefix_path)
        if best is None:
            return
        logger.info(
            "[UbisoftSession] %s holds a newer signed-in vault than the auth "
            "prefix (%+.0fs) — capturing it before seeding",
            Path(best[1]).name, best[0] - auth_mtime,
        )
        self._capture_locked(best[1])

    def retroactive_sync(self) -> dict[str, Any]:
        """Retroactive sync."""
        with self._serialised():
            return self._propagator.retroactive_sync()

    def capture(self, prefix_path: str) -> str | None:
        """Capture."""
        with self._serialised():
            return self._capture_locked(prefix_path)

    def _capture_locked(self, prefix_path: str) -> str | None:
        """:meth:`capture`, with the session lock already held."""
        if not self._reader.has_valid_credentials(prefix_path):
            return None
        auth_dir = self._config.auth_prefix_dir_expanded
        source_is_auth = (
            Path(prefix_path).resolve() == Path(auth_dir).resolve()
        )
        # Never propagate a signed-OUT session. A vault UPC has signed out of
        # holds no account, so capturing from it would carry the sign-out into
        # the auth prefix and the template — and the auth-cache artifacts
        # travel unguarded, so this has to skip the WHOLE capture, not just
        # the credential copy. (The auth prefix itself is exempt: it is the
        # source of truth and signs out legitimately.)
        #
        # Two earlier spellings of this test were both wrong, which is why
        # :meth:`_CredentialReader.is_signed_in` now decides it by reading the
        # vault: file size was the GH #435 ratchet, and "has a ``user.dat``
        # beside it" never fired at all because the template clone puts
        # ``user.dat`` in every prefix. With the guard inert, one signed-out
        # game prefix reached this point on 2026-09-05, was captured into
        # ``.upc-auth``, and was then fanned out over every other prefix.
        if not source_is_auth and self._source_is_logged_out(
            prefix_path, auth_dir,
        ):
            logger.info(
                "[UbisoftSession] capture: %s is logged-out/stale relative to "
                "the auth prefix — skipping (template + auth left intact)",
                Path(prefix_path).name,
            )
            return None
        new_mtime = self._reader.get_credential_mtime(prefix_path)
        if not new_mtime:
            return None
        stored_mtime = self._read_stored_mtime()
        credentials_changed = new_mtime > stored_mtime
        if credentials_changed:
            self._write_stored_mtime(new_mtime)
            logger.info(
                "[UbisoftSession] detected new UPC "
                "credentials (ConnectSecureStorage.dat)",
            )
        # Capture-back targets the AUTH prefix only. The template is a pristine
        # golden image that must change ONLY on an explicit sign-in or sign-out
        # — so a game/launch/uninstall capture must never write it. The one
        # exception is the sign-in path (``session_monitor`` captures from the
        # auth prefix): there the auth→auth copy self-skips and auth→template is
        # the legitimate sign-in hydration. Cloned per-game prefixes re-inject
        # from auth, so auth alone is a sufficient fresh source.
        targets = (
            (auth_dir, self._config.template_dir_expanded)
            if source_is_auth
            else (auth_dir,)
        )
        for target in targets:
            if not Path(target).is_dir():
                continue
            if Path(target).resolve() == Path(prefix_path).resolve():
                continue
            try:
                self._payload.sync_credentials_to_prefix(
                    prefix_path,
                    target,
                )
                self._payload.sync_auth_artifacts_to_prefix(
                    prefix_path,
                    target,
                )
            except Exception as e:
                logger.warning(
                    "[UbisoftSession] capture sync to %s failed: %s",
                    Path(target).name,
                    e,
                )
        if credentials_changed:
            logger.info(
                "[UbisoftSession] captured credentials → auth prefix updated",
            )
            return _CAPTURE_SENTINEL
        return None

    def stored_credential_was_rejected(self, prefix_path: str) -> bool:
        """True when UPC signed the user OUT of a prefix we had signed IN.

        We inject the auth prefix's credential into every game prefix before
        running UPC. If UPC then leaves that prefix holding a logged-out
        credential, UPC did not accept what we gave it — the stored token is
        dead server-side (Ubisoft rotates and invalidates; see the uninstall
        capture-back note). No local copy can revive it, and because
        ``capture`` correctly refuses to capture back FROM a signed-out
        prefix, nothing else notices: every later install injects the same
        dead token and the user is asked to sign in forever.

        Deliberately read-only — it reports, it does not purge. Clearing a
        user's credentials is their call (QAM → Ubisoft → Sign out), not a
        side effect of a heuristic.

        Confirmed live 2026-08-01: after a prefix reset forced UPC to run
        credential-less, the Aug-1 04:55 auth credential stopped working;
        every subsequent install injected it and prompted for sign-in.
        """
        auth_dir = self._config.auth_prefix_dir_expanded
        if not self._reader.has_valid_credentials(auth_dir):
            return False  # nothing stored → a sign-in prompt is expected
        return self._source_is_logged_out(prefix_path, auth_dir)

    def _source_is_logged_out(self, prefix_path: str, auth_dir: str) -> bool:
        """True if ``prefix_path`` holds a signed-OUT UPC session.

        Decided by shape — a vault with no ``user.dat`` beside it — not by
        comparing sizes against the auth prefix. See
        :meth:`_CredentialReader.is_signed_in` for why the size comparison had
        to go (GH #435: it froze the auth prefix on a dead token).

        ``auth_dir`` is no longer read; it stays in the signature because
        callers pass it and it keeps the two call sites symmetrical.
        """
        del auth_dir
        return not self._reader.is_signed_in(prefix_path)

    def _read_stored_mtime(self) -> float:
        """Read stored mtime."""
        session_file = self._config.upc_session_file_expanded
        if not Path(session_file).is_file():
            return 0.0
        try:
            content = (
                Path(session_file)
                .read_text(
                    encoding="utf-8",
                )
                .strip()
            )
        except OSError:
            return 0.0
        if not content.startswith("credential_mtime:"):
            return 0.0
        try:
            return float(content.split(":", 1)[1])
        except (ValueError, IndexError):
            return 0.0

    def _write_stored_mtime(self, mtime: float) -> None:
        """Write stored mtime."""
        session_file = self._config.upc_session_file_expanded
        try:
            Path(self._config.data_dir_expanded).mkdir(
                parents=True,
                exist_ok=True,
            )
            Path(session_file).write_text(
                f"credential_mtime:{mtime}\n",
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(
                "[UbisoftSession] could not write mtime marker: %s",
                e,
            )

    def clear_session_file(self) -> None:
        """Clear session file."""
        session_file = self._config.upc_session_file_expanded
        if not Path(session_file).is_file():
            return
        try:
            Path(session_file).unlink()
            logger.info(
                "[UbisoftSession] removed UPC session marker",
            )
        except OSError as e:
            logger.warning(
                "[UbisoftSession] could not remove session marker: %s",
                e,
            )
