"""Backend session hooks for a wrapper store: capture, and purge on sign-out.

py_modules/unifideck/stores/shared/wrapper_session_hooks.py

``launcher/wrapper_session`` knows *how* to move a session between prefixes.
This mixin decides *when*, on the backend side, and it exists because the
launcher cannot do it alone.

A game launch runs in the out-of-process launcher, which Steam starts and
which can be SIGKILLed with the game — so its own post-run capture is
best-effort, not a guarantee. The backend, by contrast, always sees
``GAME_STOPPED``: the frontend detects the Steam app-state change and bridges
it onto the bus via ``notify_game_stopped``. Capturing there is what makes
"the session survives a play session" true rather than usually-true.

The other two moments only the backend knows about:

* **A prefix is about to be destroyed** — uninstall, or an Install that
  rebuilds. The vendor rotates the token on every run, so a game prefix
  usually holds a *newer* session than the auth prefix; deleting it without
  capturing strands auth on a server-stale token and the next install opens
  signed-out. Ubisoft earned that lesson as a measured incident.
* **The user signed out.** Without a purge, every game prefix keeps a working
  session and the next launch quietly signs them back in.

Generic by construction: a store supplies its id and how to find its
prefixes, and the spec table supplies the rest. Adding a wrapper store is a
row in ``wrapper_session.SPECS`` plus these three small overrides.

**Battle.net is the only consumer, and Ubisoft is deliberately not one.**
That looks like drift and is not, so the reasoning is recorded here rather
than left to be rediscovered:

* ``wrapper_session.SPECS`` has exactly one row. ``spec_for("ubisoft")``
  returns ``None``, so inheriting this mixin today would give Ubisoft a
  ``GAME_STOPPED`` subscriber that returns ``False`` and does nothing —
  replacing working behaviour with a no-op.
* Ubisoft's own ``stores/ubisoft/session/`` facade is a **superset** of this
  one. It covers the same three moments (``store.py``'s stop hook,
  ``installer/uninstall.py``'s capture before deletion,
  ``auth/facade.py``'s purge on sign-out) and adds two things this mixin has
  no equivalent for: ``propagate_all_to_all`` after a capture, and
  ``stored_credential_was_rejected`` reporting a server-dead token to the
  user.
* Migrating means first **measuring** a UPC ``SessionSpec`` — its files,
  evidence, DPAPI registry keys and identity source. ``wrapper_session_specs``
  records what guessing that costs: a session whose files arrived and whose
  token did not, answered with ``ERROR_TOKEN_NOT_FOUND (49)``.

What Ubisoft did lack is the bounded wait, so that half is shared as the
free function :func:`await_client_exit` below and Ubisoft calls it directly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from unifideck.core.types.events import Events
from unifideck.event_bus.event_bus_devex import subscribe
from unifideck.launcher import wrapper_session
from unifideck.launcher.proton.handlers.wrapper_clients import (
    client_running_in,
    scan_prefix,
)

logger = logging.getLogger(__name__)

# How long to wait for the vendor client to finish exiting before reading its
# session. The client flushes its rotated token on shutdown — which is why the
# Battle.net teardown SIGTERMs before it SIGKILLs — so reading too early gets
# a torn vault. Bounded because a client that refuses to die must not hold the
# event handler open.
_EXIT_WAIT_SECONDS = 20.0
_EXIT_POLL_SECONDS = 1.0


async def await_client_exit(store: str, prefix: Path) -> None:
    """Wait, bounded, for ``store``'s vendor client in ``prefix`` to be gone.

    A free function rather than only a method on :class:`WrapperSessionHooks`
    because the wait is useful to a store that does **not** inherit the mixin.
    Ubisoft is exactly that case: it owns a richer session facade of its own
    (see this module's note below) but had no equivalent of this wait, so its
    post-play capture read the vault the instant ``GAME_STOPPED`` fired.

    Over-reporting liveness costs a short wait; under-reporting it reads a
    torn vault. ``client_running_in`` is deliberately the wider probe for
    that reason.
    """
    waited = 0.0
    while waited < _EXIT_WAIT_SECONDS:
        if not await asyncio.to_thread(client_running_in, store, prefix):
            return
        await asyncio.sleep(_EXIT_POLL_SECONDS)
        waited += _EXIT_POLL_SECONDS
    logger.info(
        "[%s] client still up in %s after %.0fs — capturing anyway",
        store, Path(prefix).name, _EXIT_WAIT_SECONDS,
    )


class WrapperSessionHooks:
    """Session capture/purge for a store whose vendor client holds the session.

    Subclasses provide :attr:`session_store_id`, :meth:`session_auth_prefix`
    and :meth:`session_prefixes`.
    """

    # -- subclass contract -------------------------------------------------

    #: Key into ``wrapper_session.SPECS``.
    session_store_id: str = ""

    def session_auth_prefix(self) -> Path:
        """The prefix the user signed into — the single source of truth."""
        raise NotImplementedError

    def session_prefixes(self) -> Iterable[Path]:
        """Every per-game prefix this store has recorded."""
        raise NotImplementedError

    def session_prefix_for(self, game_id: str) -> Path | None:
        """The prefix holding ``game_id``, or None when unknown."""
        raise NotImplementedError

    # -- internals ---------------------------------------------------------

    @property
    def _spec(self) -> wrapper_session.SessionSpec | None:
        return wrapper_session.spec_for(self.session_store_id)

    @staticmethod
    def _capture_call(
        spec: wrapper_session.SessionSpec,
        source: Path,
        auth: Path,
        busy: bool,
    ) -> bool:
        """Keyword-only call, wrapped so it can go through ``to_thread``."""
        return wrapper_session.capture(spec, source, auth, auth_busy=busy)

    def publish_session_prefixes(
        self, template: Path,
    ) -> None:
        """Tell the launcher where this store's shared prefixes live.

        The launcher runs out-of-process under the system Python and cannot
        read the backend's config, while ``prefixes_dir`` is user-configurable
        — so a path it is never told is a path it can never use. Same reason
        Battle.net writes its family codes to its id map.

        Paths only: the launcher resolves the UI locale itself through
        ``utils.locale.get_unifideck_locale``, which is the single source of
        truth for it. See ``wrapper_session.write_prefix_index``.
        """
        if not self.session_store_id:
            return
        try:
            wrapper_session.write_prefix_index(
                self.session_store_id,
                auth=self.session_auth_prefix(),
                template=template,
            )
        except Exception:
            logger.warning(
                "[%s] could not publish prefix paths for the launcher",
                self.session_store_id,
            )

    async def _await_client_exit(self, prefix: Path) -> None:
        """Wait, bounded, for the vendor client in ``prefix`` to be gone."""
        await await_client_exit(self.session_store_id, prefix)

    async def capture_session_from(self, prefix: Path) -> bool:
        """Capture ``prefix``'s session back to auth. Never raises.

        Every guard lives in ``wrapper_session.capture``: a prefix with no
        session, or one no newer than auth, is a no-op — so calling this
        speculatively is safe and callers do not have to know whether a
        rotation happened.
        """
        spec = self._spec
        if spec is None:
            return False
        auth = self.session_auth_prefix()
        try:
            # A token kept in the registry cannot be written under a live
            # wineserver — it would rewrite the file from memory on exit and
            # discard us silently. Report the destination's state honestly.
            busy = bool(await asyncio.to_thread(
                scan_prefix, auth,
            ))
            return await asyncio.to_thread(
                self._capture_call, spec, Path(prefix), auth, busy,
            )
        except Exception:
            logger.warning(
                "[%s] session capture from %s failed",
                self.session_store_id, Path(prefix).name,
            )
            return False

    async def capture_before_prefix_loss(self, prefix: Path | None) -> bool:
        """Capture from a prefix that is about to be deleted or rebuilt.

        Called by uninstall and by the Install reset. The prefix usually holds
        a newer token than auth, because the vendor rotates on every run and
        the launcher's own capture is best-effort — so this is the last chance
        to keep it.
        """
        if prefix is None:
            return False
        return await self.capture_session_from(Path(prefix))

    async def purge_session_everywhere(self) -> int:
        """Remove the session from the template and every game prefix.

        For sign-out. The auth prefix is left alone: it is the source of truth
        and the store's own sign-out path decides what happens to it (for a
        store whose prefix holds the user's games, that is deliberately
        nothing).
        """
        spec = self._spec
        if spec is None:
            return 0
        template = wrapper_session.template_prefix(self.session_store_id)
        targets = [*self.session_prefixes()]
        if template is not None:
            targets.append(template)
        purged = 0
        for target in targets:
            try:
                purged += await asyncio.to_thread(
                    wrapper_session.purge, spec, Path(target),
                )
            except Exception:
                logger.warning(
                    "[%s] could not purge the session from %s",
                    self.session_store_id, Path(target).name,
                )
        return purged

    # -- bus ---------------------------------------------------------------

    @subscribe(Events.GAME_STOPPED)
    async def _capture_wrapper_session_on_stop(self, **kwargs: Any) -> None:
        """Capture the token the vendor rotated during play back to auth.

        The one hook that always fires. A launch runs in the launcher
        subprocess, which can be SIGKILLed along with the game (the Steam stop
        button and the QAM "X" both take that path), so its own capture cannot
        be relied on. Left uncaptured, auth ends up on a server-stale token
        and the next install or launch of a *different* game opens signed-out
        — which is the reported symptom.

        Guarded end to end, so a run that rotated nothing is a cheap no-op.
        """
        if kwargs.get("store") != self.session_store_id:
            return
        game_id = kwargs.get("game_id")
        if not isinstance(game_id, str) or not game_id:
            return
        prefix = self.session_prefix_for(game_id)
        if prefix is None:
            return
        await self._await_client_exit(prefix)
        if await self.capture_session_from(prefix):
            logger.info(
                "[%s] captured the rotated session after play for %s",
                self.session_store_id, game_id,
            )
