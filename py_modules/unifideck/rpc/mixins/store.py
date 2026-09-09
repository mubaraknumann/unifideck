"""StoreRPCMixin — store auth + login-state RPC.

Covers the auth surface only. The neighbouring surfaces live in
sibling mixins, all composed onto ``Plugin`` in ``main.py``:

* library and sync — ``SyncRPCMixin``;
* install and update — ``DownloadRPCMixin``;
* auth-shortcut context (``get_<store>_auth_shortcut_context``,
  ``get_compat_tool_for_game``) — ``AuthShortcutsRPCMixin``, split
  out to keep this file under the 200 LOC ceiling.

Composition is flat: every mixin is a base of ``Plugin`` and reaches
its dependencies through ``self``. There is no handler-group layer and
no ``rpc/handlers/`` package; earlier versions of this docstring
described a structure that was never built.
"""

from __future__ import annotations

import logging
from typing import Any

from ._compat_payload import active_track, slim_cache_entry

logger = logging.getLogger(__name__)

class StoreRPCMixin:
    """Store-auth RPC: start/check/clear flows + login status."""

    registry: Any

    async def store_auth(self, store: str, action: str) -> Any:
        """Run one step of a store's auth flow.

        Forwards to ``registry.auth_action`` which knows the
        per-store wiring. Every store now authenticates through the
        Steam-shortcut / launcher flow (``"start"`` kicks the backend
        prep; the launcher captures credentials and the session
        monitor emits ``STORE_AUTH_COMPLETE``), so the only actions
        the frontend sends are ``"start"`` and ``"logout"``. The old
        ``"complete"`` + ``{code}`` 2FA path (a relic of Ubisoft's
        former API login) has been removed — Ubisoft Connect handles
        its own sign-in inside the UPC prefix now.

        Args:
            store: store identifier.
            action: per-store action name (``"start"`` / ``"logout"``).

        Returns:
            Per-store auth result dict.
        """
        logger.info("[StoreAuth:%s] action=%s", store, action)
        result = await self.registry.auth_action(store, action)
        success = getattr(result, "success", None)
        if success is None and isinstance(result, dict):
            success = result.get("success")
        error = getattr(result, "error", None)
        if error is None and isinstance(result, dict):
            error = result.get("error")
        logger.info(
            "[StoreAuth:%s] action=%s success=%s error=%s",
            store, action, success, error,
        )
        return result

    async def connect_gamevault(
        self,
        server_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        download_dir: str = "",
    ) -> Any:
        """Sign in to a self-hosted GameVault server.

        A route of its own rather than a payload bolted onto
        :meth:`store_auth`. That method takes ``(store, action)`` and nothing
        else on purpose: its old ``"complete"`` + ``{code}`` kwargs channel
        was removed with Ubisoft's API login, and re-opening a generic
        passthrough so one store can smuggle credentials through it would
        undo that for every store. GameVault is the only connector whose
        sign-in is a form rather than a browser or a Steam shortcut — it does
        not go through ``AuthDispatcher`` on the frontend either — so the one
        flow that needs a payload gets one named, typed entry point.

        Args:
            server_url: base URL of the user's GameVault server.
            username: GameVault account name.
            password: GameVault password. Never logged; the store persists
                it 0600 because the server issues no refresh grant, so the
                credentials are what re-authenticate unattended.
            verify_ssl: False to accept a self-signed certificate, which a
                LAN server commonly has.
            download_dir: staging directory for downloaded archives. Empty
                means the configured default.

        Returns:
            ``AuthResult`` — ``success`` plus ``error`` when it failed.
        """
        logger.info(
            "[StoreAuth:gamevault] connect server=%s user=%s verify_ssl=%s "
            "download_dir=%s",
            server_url, username, verify_ssl, download_dir or "(default)",
        )
        result = await self.registry.auth_action(
            "gamevault",
            "start",
            server_url=server_url,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
            download_dir=download_dir or None,
        )
        logger.info(
            "[StoreAuth:gamevault] connect success=%s error=%s",
            getattr(result, "success", None),
            getattr(result, "error", None),
        )
        return result

    async def connect_gamevault_local(self, vault_dir: str = "") -> Any:
        """Use a folder of game archives on this device as the library.

        The offline half of GameVault: no server, no account, no network.
        The user picks a folder, drops archives in it, and they appear in
        the library on the next sync.

        A route of its own rather than a ``mode`` argument on
        :meth:`connect_gamevault`, because the two parameter sets are
        disjoint — one takes a URL and a password, the other a path — and a
        single route would have to accept both and validate that exactly one
        set arrived. The store still reaches the same
        ``registry.auth_action("gamevault", "start", ...)`` entry point, so
        there is one auth path underneath, not two.

        There is deliberately **no install-location argument**. Every install
        already goes through the shared storage picker, which knows about SD
        cards and USB drives and applies to all seven stores; a per-store
        copy of that setting would be a second answer to a question already
        answered, and the two would disagree the first time one was changed.

        Args:
            vault_dir: folder the user will drop game archives into. Empty
                means the configured default, so connecting with an untouched
                form is a complete action — the folder is created for the
                user rather than demanded from them. Created along with a
                marker file that lets a later sync tell an empty vault from
                an unmounted drive.

        Returns:
            ``AuthResult`` — ``success`` plus ``error`` when it failed.
        """
        logger.info("[StoreAuth:gamevault] connect local vault=%s", vault_dir)
        result = await self.registry.auth_action(
            "gamevault", "start", mode="local", vault_dir=vault_dir,
        )
        logger.info(
            "[StoreAuth:gamevault] connect local success=%s error=%s",
            getattr(result, "success", None),
            getattr(result, "error", None),
        )
        return result

    async def check_store_status(self) -> Any:
        """Probe every registered store for its current login state.

        Used by the stores tab to render the per-store
        login-status badges. The registry parallelises the
        probes internally.

        Returns:
            List of per-store status dicts.
        """
        return await self.registry.check_all_status()

    async def get_store_infos(self) -> Any:
        """Return the static metadata (id, name, icon) for every store.

        Synchronous on the registry side — pulled from the
        bundled store descriptors at registration time.

        Returns:
            List of store-info dicts.
        """
        return self.registry.get_store_infos()

    async def prepare_store_web_session(self, store_id: str) -> Any:
        """Give the browser a signed-in session for ``store_id``, if it can.

        Called just before the QAM cart opens a store's shop. Only
        Amazon implements it: nile signs in through Amazon's device
        registration flow, which authorises the device but leaves the
        shared Edge profile without the auth cookies a signed-in
        amazon.com needs, so the shop opened logged out. Every other
        browser store signs in through an ordinary web login that
        leaves its own session behind.

        Must run BEFORE Edge launches — it writes the profile's cookie
        DB, which Edge owns and rewrites while running.

        Never raises. A store with nothing to do, an unknown store, and
        a failed exchange all answer the same way: the shop still
        opens, with whatever session the profile already had.
        """
        store = self.registry.get_store(store_id)
        if store is None or not hasattr(store, "prepare_web_session"):
            return {"success": False, "error": "not_applicable"}
        try:
            return await store.prepare_web_session()
        except Exception:
            logger.exception(
                "[StoreRPC:web_session:%s] preparation failed", store_id,
            )
            return {"success": False, "error": "prepare_failed"}

    async def clear_store_auths(self) -> Any:
        """Sign out of every store and wipe cached credentials.

        Loud admin action: requires user confirmation in
        the UI. Delegates to ``registry.logout_all`` which
        iterates and calls each store's logout method.

        Returns:
            Per-store outcome dict.
        """
        return await self.registry.logout_all()

    cache: Any
    services: Any

    async def get_real_steam_appid_mappings(self) -> dict[str, Any]:
        """Return ``{shortcut_app_id: real_steam_app_id}`` for every non-Steam game.

        Populated by :class:`MetadataService` on every sync via
        ``fetch_appdetails_for_game``. The frontend
        ``SteamStorePatcher`` reads this map at boot to know which
        synthetic shortcut IDs should be redirected to which real
        Steam Store entries.

        Returns:
            ``{"success": bool, "mappings": {str → int}}``.
        """
        stores = getattr(self.cache, "_stores", None)
        if not isinstance(stores, dict):
            return {"success": False, "mappings": {}}
        store = stores.get("steam_real_appid")
        data = getattr(store, "_data", None)
        if not isinstance(data, dict):
            return {"success": True, "mappings": {}}
        mappings: dict[str, int] = {}
        for k, v in data.items():
            try:
                mappings[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return {"success": True, "mappings": mappings}

    async def get_steam_metadata_cache(self) -> dict[str, Any]:
        """Return ``{steam_app_id: appdetails_dict}`` for every cached entry.

        The ``appdetails`` dicts are the raw Steam Store JSON
        (description, screenshots, developers, publishers,
        categories, genres, achievements, dlc, controller
        support, platforms, languages). Read by the frontend
        ``SteamStorePatcher`` to spoof non-Steam shortcuts as
        Steam Store games in Steam's UI.

        Returns:
            ``{"success": bool, "metadata": {str → dict}}``.
        """
        stores = getattr(self.cache, "_stores", None)
        if not isinstance(stores, dict):
            return {"success": False, "metadata": {}}
        store = stores.get("steam_metadata")
        data = getattr(store, "_data", None)
        if not isinstance(data, dict):
            return {"success": True, "metadata": {}}
        return {
            "success": True,
            "metadata": {str(k): v for k, v in data.items()},
        }

    # ``inject_game_to_appinfo`` lived here. It was a stub: it logged and
    # returned ``{"success": True, "deferred": True}``, and its own docstring
    # admitted the success value existed only so the frontend's
    # fire-and-forget call would not log a failure on every navigation.
    #
    # It had a live caller, which is why the §1.2 dead-RPC sweep did not see
    # it — two, in fact: AppDetailsPatch on open, and the patched
    # ``GetAppOverviewByAppID`` getter, which runs on every overview read
    # across the whole library. So it cost a round-trip per read, forever, to
    # do nothing.
    #
    # The persistence it promised was redundant rather than missing:
    # ``applyAppStorePatch`` awaits ``loadFromBackend()`` and re-spoofs from
    # the backend cache on every plugin load, so surviving a Steam restart is
    # handled by re-patching, not by writing ``appinfo.vdf``. Audit §2.8
    # bullet 4, register item 35.

    async def get_protondb_cache(self) -> dict[str, Any]:
        """Return every cached rating, resolved for **this** device.

        Used by the frontend ``protondb-cache`` module to populate the
        in-memory rating lookup that drives compat badges and the
        ``deckCompat`` library-tab filter. Reads the ``compat`` cache
        namespace populated by :class:`CompatLibrary` — never triggers
        a fresh network fetch from here.

        The active device's track is resolved **here** rather than in
        the frontend: ``loadDeviceType()`` is async and can answer after
        this payload has been consumed, which would mis-filter the first
        render of the compatibility tab on a Steam Machine.

        Each row is projected down to what the frontend reads, which
        makes this smaller than the whole cached entry it used to
        return while carrying strictly more information.

        Returns:
            Mapping of ``str(app_id)`` →
            ``{"title": str, "protondb_tier": str | None,
              "compat_status": str, "sources": list[str]}``.
            Empty dict when the cache is cold or unregistered.
        """
        stores = getattr(self.cache, "_stores", None)
        if not isinstance(stores, dict):
            return {}
        compat_store = stores.get("compat")
        data = getattr(compat_store, "_data", None)
        if not isinstance(data, dict):
            return {}
        track = active_track()
        return {
            key: slim_cache_entry(entry, track)
            for key, entry in data.items()
            if isinstance(entry, dict)
        }
