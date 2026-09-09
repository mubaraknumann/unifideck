"""Battle.net store — the ``StoreBase`` implementation.

py_modules/unifideck/stores/battlenet/store.py

A launcher-wrapper store: the real Battle.net Windows client runs inside a
Proton prefix and does the downloading and launching. There is no CLI and
there will not be one — Heroic requires a CLI to exist before adding a
store, and none of the NGDP projects ship a downloader.

Ownership is read from the **client's own local state**, not from the web:
``CachedData.db`` holds the account's licence ids, and the cached PUB
catalog turns them into playable titles by evaluating a small rule
language. Those rules also need ``game_account`` facts for free-to-play and
subscription titles, which would come from ``games-and-subs``. **Nothing
fetches them**, so those titles are missing — see
:meth:`_game_account_programs` and audit §3.5 finding A.

Consequence: the library is unknown until the user has signed into the
client once. That is not a new constraint — install and launch already
require it.

Delegation only. Every concern lives in its own module (``ownership/``,
``prefix/``, ``library``, ``id_map``), mirroring the Ubisoft layout.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.types.domain import Game, StoreInfo
from unifideck.core.types.results import AuthResult, InstallResult, Result
from unifideck.event_bus.event_bus_devex import auto_wire
from unifideck.launcher import wrapper_session
from unifideck.launcher.proton.handlers import battlenet_login_state as login_state
from unifideck.stores.shared.auth_shortcut import (
    AuthShortcutSpec,
    build_context,
)
from unifideck.stores.shared.store_base import StoreBase
from unifideck.stores.shared.wrapper_auth_monitor import WrapperAuthMonitor
from unifideck.stores.shared.wrapper_session_hooks import WrapperSessionHooks

from . import config as store_config
from . import library as library_mod
from . import paths
from .id_map import BattlenetIdMap
from .install import BattlenetInstaller
from .prefix import BattlenetPrefixManager, inspect_prefix

if TYPE_CHECKING:
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus import EventBus

logger = logging.getLogger(__name__)


class BattlenetStore(WrapperSessionHooks, StoreBase):
    """Blizzard Battle.net, driven through the vendor client in a prefix."""

    session_store_id = "battlenet"

    store_info = StoreInfo(
        name="battlenet",
        display_name="Battle.net",
        auth_method="shortcut",
        icon_asset="battlenet.png",
        supports_install=True,
    )

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        plugin_dir: str | None = None,
        config: Any | None = None,
    ) -> None:
        super().__init__(bus, cache, plugin_dir, config)
        self.config = store_config.from_config_manager(config)
        self.prefixes = BattlenetPrefixManager(self.config.prefixes_dir_path)
        self.id_map = BattlenetIdMap(self.config.id_map_path)
        self._installer = BattlenetInstaller(
            self.prefixes, self.id_map, self.capture_before_prefix_loss,
        )
        # Injected post-discovery by services/bootstrap/store_injector.py.
        self._shortcut_service: Any | None = None
        # The sign-in happens in a detached client we never see the exit of, so
        # this is the only thing that reports a verdict. Without it the
        # frontend's AuthDispatcher waits on an event nobody emits and holds
        # the Sign In button dead for its full 10-minute timeout — the tester
        # report that reads "it only worked again after I restarted Steam".
        self._auth_monitor = WrapperAuthMonitor(
            store="battlenet",
            is_signed_in=self._auth_session_landed,
            on_captured=self._on_auth_captured,
            bus=bus,
        )
        # Credential fingerprint as it was when the current sign-in started.
        self._auth_baseline: tuple[float, int] = (0.0, 0)
        # Tell the out-of-process launcher where the shared prefixes are: it
        # runs under the system Python, cannot read our config, and needs the
        # auth prefix to inject the live session before it starts a client.
        # The UI locale is deliberately NOT published here: the launcher
        # resolves it itself through ``utils.locale.get_unifideck_locale``,
        # so there is one answer rather than a cached second one.
        self.publish_session_prefixes(self.prefixes.template_prefix)
        # Subscribes ``GAME_STOPPED`` so the token the client rotates during a
        # play session is captured back to the auth prefix.
        auto_wire(self, bus)

    # -- WrapperSessionHooks ----------------------------------------------

    def session_auth_prefix(self) -> Path:
        return self.prefixes.auth_prefix

    def session_prefixes(self) -> list[Path]:
        return list(self.id_map.all_prefix_paths())

    def session_prefix_for(self, game_id: str) -> Path | None:
        return self.id_map.resolve_prefix(game_id)

    # -- helpers -----------------------------------------------------------

    @property
    def _auth_drive_c(self) -> Path | None:
        """drive_c of the prefix the user signed into, if it exists."""
        return paths.drive_c(self.prefixes.auth_prefix)

    # ``prefix_env``/``prefix_name`` are load-bearing: without them the
    # launcher derives the prefix from ``ctx.game_id`` ("bnet-auth") and
    # signs the user in to an empty ``prefixes/battlenet/bnet-auth`` while
    # the client lives in ``.bnet-auth``. Same token Ubisoft passes for UPC.
    AUTH_SHORTCUT = AuthShortcutSpec(
        store="battlenet",
        store_game_id="battlenet:bnet-auth",
        display_name="Battle.net",
        action_env="UNIFIDECK_BATTLENET_ACTION",
        prefix_env="UNIFIDECK_BATTLENET_PREFIX_NAME",
        prefix_name=paths.AUTH_PREFIX_NAME,
    )

    async def get_auth_shortcut_context(self) -> dict[str, Any]:
        """Payload the frontend needs to RunGame the sign-in shortcut.

        Without this the client can only be opened in Desktop Mode: in
        Gaming Mode a process with no Steam shortcut gets no gamescope
        session and its window never renders.
        """
        return await build_context(
            self._shortcut_service, self.AUTH_SHORTCUT, self._plugin_dir,
            bus=self._bus,   # lets a first-time write fetch its tile artwork
        )

    def _launcher_path(self) -> str:
        base = Path(self._plugin_dir) if self._plugin_dir else Path()
        return str(base / "bin" / "unifideck-launcher")

    def _game_account_programs(self) -> frozenset[str]:
        """Programs the account has a game account for.

        **Always empty today: a gap, not a safe default.** Nothing writes
        the ``game_accounts`` cache this reads — the consumer shipped, the
        producer never did (§3.5 A) — so every free-to-play and
        subscription title is dropped. ``library.py``'s header measures 17
        programs from licences against 22 with game accounts;
        ``count_game_account_gated`` logs the loss each sync.
        """
        cached = self._cached_game_accounts()
        return frozenset(cached)

    def _cached_game_accounts(self) -> set[str]:
        try:
            raw = self._cache.get("battlenet", "game_accounts")
        except Exception:  # cache miss must never break a library read
            return set()
        return set(raw) if isinstance(raw, (list, set, tuple)) else set()

    # -- StoreBase ---------------------------------------------------------

    async def is_available(self) -> bool:
        """Signed in when the client prefix holds a usable licence ledger.

        Keyed on the *auth* prefix rather than on any game prefix, so a
        user who has connected but not installed anything still shows as
        connected.
        """
        drive_c = self._auth_drive_c
        if drive_c is None or self._signed_out_marker.exists():
            self._cached_available = False
            return False
        from .ownership import read_licences

        # ``StoreRegistry.available()`` reads this attribute rather than
        # calling us, so a store that never sets it is never "available".
        self._cached_available = read_licences(drive_c).is_usable
        return self._cached_available

    async def _on_auth_captured(self) -> None:
        """Clear the signed-out marker — only on confirmed success, never in ``start_auth``."""
        self._cached_available = True
        with contextlib.suppress(OSError):
            self._signed_out_marker.unlink(missing_ok=True)

    async def _auth_session_landed(self) -> bool:
        """Has a *new* session appeared in the auth prefix?

        Not ``is_available`` — the licence ledger survives sign-out, so it
        would answer True for a sign-in that has not happened yet. The
        marker is only cleared on confirmed success (``_on_auth_captured``),
        so ``is_available`` keeps answering False until the monitor confirms.

        Keyed on the credential material proper, including the registry
        token. The baseline distinguishes "already there" from "just landed";
        the client log vetoes a fingerprint change from a non-sign-in write
        (the registry keys survive a failed login and the client rewrites
        ``account.db`` to remember the account it is prompting for).
        ``UNKNOWN`` does not veto."""
        spec = wrapper_session.spec_for(self.session_store_id)
        if spec is None:
            return False
        prefix = self.prefixes.auth_prefix
        if not await asyncio.to_thread(wrapper_session.has_session, spec, prefix):
            return False
        current = await asyncio.to_thread(wrapper_session.fingerprint, spec, prefix)
        if current == self._auth_baseline:
            return False
        state = await asyncio.to_thread(login_state.read_login_state, prefix)
        return state is not login_state.LoginState.SIGNED_OUT

    @property
    def _signed_out_marker(self) -> Path:
        """Set by logout, cleared by a successful sign-in.

        Needed because nothing on disk distinguishes "signed in" from
        "signed out but remembered". Measured across three prefixes: the
        licence ledger AND the ``login_cache`` battle tag both survive a
        sign-out — they are a cache of the last account, which is how the
        client pre-fills the login form. Keying availability on either one
        means the store reports connected forever.

        A marker rather than deleting the prefix, because for this store the
        prefix holds the user's installed games.
        """
        return self.config.prefixes_dir_path / ".unifideck_signed_out"

    async def start_auth(self, **_kwargs: Any) -> AuthResult:
        """Open the vendor client so the user can sign in.

        The client login is the primary credential: it produces both the
        licence ledger and the cached catalog. The frontend drives this by
        RunGame-ing an auth shortcut, because a backend-spawned process has
        no gamescope session in Gaming Mode.

        The signed-out marker is deliberately NOT cleared here. It is cleared
        by the monitor's ``on_captured`` hook once a session actually lands,
        because clearing it up front and then timing out would leave the
        store reporting "available" when no sign-in happened — the marker gone
        but the previous session's licence ledger still present. Instead,
        ``is_available`` keeps returning False until the monitor confirms
        success. The baseline fingerprint is needed so the monitor knows the
        difference between "session already there" and "session just arrived."
        """
        status = inspect_prefix(self.prefixes.auth_prefix)
        if not status.usable:
            # Deliberately NOT installed here. ``AuthDispatcher.kickAndLaunch``
            # awaits this RPC *before* it RunGame-s the auth shortcut, so
            # anything slow here delays the launcher and anything that blocks
            # here stops it running at all. That is precisely what happened:
            # the installer opened a wizard, in Gaming Mode it had no
            # gamescope session to render into, this call never returned, and
            # the launcher never started — a Sign In button that did nothing.
            #
            # The install now happens behind RunGame, in
            # ``battlenet_auth_launch``, which is the rule
            # ``services/download/wrapper_signals.py`` already states: the
            # backend must not spawn the vendor client itself. Its installer
            # is no exception.
            logger.info(
                "[Battlenet] auth prefix has no client — the sign-in shortcut "
                "will install it",
            )
        # Creating the watcher task is safe here; *probing* would not be. The
        # comment above applies in full — this call blocks the shortcut launch,
        # so the monitor must start and return, never poll inline. One stat
        # sweep of the credential files is the whole cost.
        spec = wrapper_session.spec_for(self.session_store_id)
        self._auth_baseline = (
            (0.0, 0) if spec is None
            else await asyncio.to_thread(wrapper_session.fingerprint, spec, self.prefixes.auth_prefix)
        )
        await self._auth_monitor.start()
        return AuthResult(
            success=True,
            store=self.store_name,
            next_step="client_login",
            metadata={
                "pending": True,
                "prefix": str(self.prefixes.auth_prefix),
                "needs_bootstrap": not status.usable,
            },
        )

    async def complete_auth(self, **_kwargs: Any) -> AuthResult:
        signed_in = await self.is_available()
        return AuthResult(
            success=signed_in,
            store=self.store_name,
            error=None if signed_in else "Battle.net client is not signed in",
            error_code=None if signed_in else "not_signed_in",
        )

    async def logout(self) -> Result:
        """Forget cached account state. Never touches a prefix.

        Deliberately non-destructive, and the opposite of Ubisoft's logout:
        for this store the prefix *is* the install, so wiping prefixes here
        would delete the user's games. Signing the client out is a separate,
        explicitly-labelled action.

        The *session* is a different matter from the prefix. Every game prefix
        holds a working copy of it, so without a purge the next launch opens a
        client that is still signed in and the sign-out silently did nothing.
        Only session files are removed; the games are untouched.
        """
        # A sign-in still being watched is moot once the user signs out, and
        # leaving it running would let it emit STORE_AUTH_COMPLETE against the
        # session we are about to purge.
        await self._auth_monitor.stop()
        purged = await self.purge_session_everywhere()
        if purged:
            logger.info(
                "[Battlenet] removed %d session file(s) from the template and "
                "game prefixes", purged,
            )
        try:
            self._cache.clear("battlenet")
        except Exception:
            logger.warning("[Battlenet] cache invalidate failed during logout")
        try:
            self._signed_out_marker.parent.mkdir(parents=True, exist_ok=True)
            self._signed_out_marker.touch()
        except OSError:
            logger.warning("[Battlenet] could not record the signed-out state")
        # STORE_LOGOUT is emitted by ``StoreRegistry.auth_action`` on a
        # successful logout, which is the only path that reaches here.
        # Emitting it again would deliver the event twice.
        self._cached_available = False
        logger.info(
            "[Battlenet] signed out (prefixes untouched — they hold the games)",
        )
        return Result(success=True, store=self.store_name)

    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        """Owned + installed titles, read entirely from client-local state.

        Returns ``None`` — not ``[]`` — when that state cannot be read.
        Every fact here lives in the client's Wine prefix, so a missing
        prefix or an empty catalog cache means *we don't know what you
        own*, not *you own nothing*. The sync layer treats ``[]`` as
        authoritative and lets reconcile delete every Battle.net shortcut;
        ``None`` reports ``library_unreadable`` and keeps them (§3.5 B).
        """
        drive_c = self._auth_drive_c
        if drive_c is None:
            logger.info("[Battlenet] no client prefix yet — library unknown")
            return None

        games = await library_mod.read_library(
            drive_c,
            game_account_programs=self._game_account_programs(),
            collect_installed=self._collect_installed,
            launcher_path=self._launcher_path(),
        )
        if games is None:
            return None
        self._record_families(games)
        logger.info(
            "[Battlenet] library: %d titles (%d installed, force=%s)",
            len(games),
            sum(1 for g in games if g.installed),
            force,
        )
        return games

    def _record_families(self, games: list[Game]) -> None:
        """Persist each title's ``--exec`` family code to the id map.

        The launcher runs out-of-process under the system Python and cannot
        reach the catalog, so a family it is never told is a family it can
        never use — and Battle.net's failure mode for a missing or obsolete
        family is *silent*. Doing this at sync (rather than at install) is
        what makes a title launchable without a prior install, and is the
        only writer that sees the whole library.
        """
        changed = library_mod.record_families(self.id_map, games)
        if changed:
            logger.info("[Battlenet] recorded family codes for %d title(s)", changed)

    def _collect_installed(self) -> dict[str, Any]:
        """Install state across every prefix we have recorded."""
        merged: dict[str, Any] = {}
        for prefix in self.id_map.all_prefix_paths():
            drive_c = paths.drive_c(prefix)
            if drive_c is None:
                continue
            merged.update(library_mod.read_install_state(drive_c, prefix))
        return merged

    async def install_game(
        self, game_id: str, *, install_path: str | None = None, **kwargs: Any,
    ) -> InstallResult:
        """Prepare the prefix, then block until the client has installed the game.

        ``--exec="install <FAMILY>"`` does **not** start a download — that
        was measured against the current client with a known-good family
        code. The install is a user click inside the client, exactly as it
        is for Ubisoft, so this prepares the prefix, asks the frontend to
        bring the client up (``on_ready``) and then watches for the game.

        ``install_path`` is the storage location the user picked. The game
        installs *inside* the prefix, so placing the prefix there is the only
        thing that puts the game on that disk — and the only way the client's
        own free-space check reads the right volume.
        """
        return await self._installer.install(
            game_id,
            install_path,
            progress_cb=kwargs.get("progress_cb"),
            on_ready=kwargs.get("on_ready"),
        )

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        """Remove the game by removing its prefix — the install lives inside."""
        delete_prefix = bool(kwargs.get("delete_prefix", True))
        prefix = self.id_map.resolve_prefix(game_id)
        if prefix is None:
            return Result(
                success=False,
                store=self.store_name,
                error="No recorded prefix for this game",
                error_code="prefix_unknown",
            )
        # Last chance to keep this prefix's session. The client rotates the
        # token on every run, so a played game usually holds a NEWER one than
        # the auth prefix; deleting it uncaptured strands auth on a stale
        # token and the next install opens signed-out.
        await self.capture_before_prefix_loss(prefix)
        if delete_prefix and not self.prefixes.remove_game_prefix(prefix):
            return Result(
                success=False,
                store=self.store_name,
                error="Refused to remove a prefix Unifideck did not create",
                error_code="prefix_not_owned",
            )
        self.id_map.forget(game_id)
        from unifideck.core.types.events import Events

        await self._emit(Events.GAME_UNINSTALLED, store=self.store_name, game_id=game_id)
        return Result(success=True, store=self.store_name)

    async def update_game(self, game_id: str, **kwargs: Any) -> InstallResult:
        """Updates are client-driven, same shape as install.

        Same blocking wait, and for the same reason: reporting success the
        moment the prefix resolved is what put a Play button on a game that
        had not downloaded. It reuses the existing prefix rather than going
        through the install path, which resets it — here that would delete
        the very game being updated.
        """
        return await self._installer.update(
            game_id,
            progress_cb=kwargs.get("progress_cb"),
            on_ready=kwargs.get("on_ready"),
        )

    async def check_for_updates(self) -> list[str]:
        """Not implemented: the client applies updates on its own.

        ``product.db`` exposes the installed version, but there is no
        authenticated per-account source for the *available* version that
        does not go through the client. Reporting a guess would produce
        phantom update badges.
        """
        return []

    async def get_game_size(self, game_id: str) -> int | None:
        """Total install size, when the client has finished writing it.

        Comes from ``product.db``, which populates the total only at
        completion — during a download it is 0, meaning "not known yet"
        rather than "empty".
        """
        record = self.id_map.get(game_id)
        if record and record.total_bytes:
            return record.total_bytes
        row = await self._install_row(game_id)
        return row.total_bytes if row else None

    def get_prefix_path(self, game_id: str) -> str | None:
        """The game's Wine prefix — for this store, the whole install footprint.

        The game lives *inside* the prefix and uninstall removes the prefix, so
        this is the directory whose bytes are the disk space the game actually
        occupies. See ``stores/shared/installed_size.resolve_size_root``.
        """
        prefix = self.id_map.resolve_prefix(game_id)
        return str(prefix) if prefix else None

    def find_installed_exe(
        self, install_path: str, game_id: str | None = None,
    ) -> str | None:
        """The game's executable, from the client's own records.

        The download worker prefers a store-specific resolver and otherwise
        guesses with a generic heuristic — which inside a Blizzard install
        directory picks up launchers, crash handlers and updaters as readily
        as the game.
        """
        del install_path
        if not game_id:
            return None
        record = self.id_map.get(game_id)
        return record.exe_path if record else None

    async def get_installed_path(self, game_id: str) -> str | None:
        """Host-side install directory, translated out of Wine syntax."""
        record = self.id_map.get(game_id)
        if record and record.install_path:
            return record.install_path
        row = await self._install_row(game_id)
        return row.host_install_path if row else None

    async def _install_row(self, game_id: str) -> Any | None:
        """This game's row in the client's install records, or ``None``."""
        return await library_mod.install_row(
            game_id, self.id_map.resolve_prefix(game_id),
        )
