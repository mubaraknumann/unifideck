"""Domain dataclasses — Game, StoreInfo, CLITool.

The three core domain records used everywhere across the
plugin:

* ``Game``      — the cross-store unified game record;
* ``StoreInfo`` — static descriptor of one store (display
  name, auth method, capability flags);
* ``CLITool``   — descriptor of an external CLI dependency
  (legendary, nile, umu-run, …) used by ``bin/binary_resolver``.

Plain ``@dataclass`` (not frozen) — fields are mutable so the
sync service can update ``installed`` / ``install_path``
in place after a successful install detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Game:
    """Cross-store unified game record.

    Attributes:
        app_id: Steam-style AppID (deterministic from store
            + game_id + title; collision-free across stores).
        store: store identifier (``"epic"``, ``"gog"``, …).
        store_game_id: store-native game id, used to call
            back into the store's RPC.
        title: human-readable title for display.
        installed: True if a local install directory was
            detected.
        install_path: absolute path to the install directory,
            or ``None`` when not installed.
        exe_path: detected launcher executable, or ``None``
            when not yet resolved.
        size_bytes: on-disk size of the install directory,
            0 when unknown.
        tags: list of game tags (genre, feature flags).
        icon_url / hero_url / logo_url: artwork URLs from
            the metadata service, ``None`` until enriched.
        metadata: free-form dict for store-specific extras
            (release date, last-played, etc.).
    """

    app_id: int
    store: str
    store_game_id: str
    title: str
    installed: bool = False
    install_path: str | None = None
    exe_path: str | None = None
    size_bytes: int = 0
    tags: list[str] = field(default_factory=list)
    icon_url: str | None = None
    hero_url: str | None = None
    logo_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class StoreInfo:
    """Static descriptor for one store.

    Returned by ``StoreRegistry.get_store_infos`` so the
    frontend can render the per-store badges + capability
    chips without RPC round-trips per store.

    **Two payload keys are deliberately NOT fields here.**
    ``get_store_infos`` injects ``available`` (live probe
    state) and ``client_runs_in_prefix`` (derived from
    ``launcher.wrapper_stores.WRAPPER_STORES``) into the dict
    it builds. ``client_runs_in_prefix`` used to be a
    hand-written ``uses_wine`` field on every store, a second
    copy of a fact ``WRAPPER_STORES`` already owned — see
    audit §3.1. Deriving it is only possible outside this
    module: ``core.types`` may not import ``launcher`` (the
    ``types-is-leaf`` import-linter contract). Leaving it off
    the dataclass is what makes re-adding the copy fail loudly
    at construction instead of being silently overwritten.

    Attributes:
        name: internal id (``"epic"``).
        display_name: localised name shown to the user
            (``"Epic Games"``).
        auth_method: identifier used by the auth UI (e.g.
            ``"oauth"``, ``"cdp"``).
        icon_asset: asset path or URL of the store logo.
        supports_install: True if the store can install games
            locally (False for streaming-only services).

    Capability flags (achievements, cloud saves, language picker, browser
    storefront) are **not** fields here. They are injected into the
    ``get_store_infos`` payload from ``core.store_capabilities``, so a store
    cannot declare an answer that disagrees with the code implementing it.
    See the note below ``supports_install``.
    """

    name: str
    display_name: str
    auth_method: str
    icon_asset: str
    supports_install: bool = True

    # There is deliberately no ``supports_cloud_saves`` field. It was one,
    # and only Battle.net ever declared it — as ``False`` — so GOG and Epic,
    # the only two stores that HAVE cloud saves, both took the default and
    # advertised that they did not. Nothing read it, which is the only reason
    # it never broke anything; wiring a UI to it would have hidden the
    # feature on exactly the stores that support it.
    #
    # It is now derived in ``get_store_infos`` from
    # ``core.store_capabilities.CLOUD_SAVE_STORES``, which is also what
    # ``CloudSaveService`` registers its strategies from. Leaving the field
    # off means a re-added literal raises ``TypeError`` at construction —
    # the §3.1 ``uses_wine`` precedent: make the duplicate impossible rather
    # than check it.

@dataclass
class CLITool:
    """Descriptor for an external CLI dependency.

    Used by ``core.binaries.binary_resolver`` to locate bundled CLI tools
    (legendary, gogdl, nile). The resolver iterates ``search_paths`` until it
    finds an executable file, and verifies it against the SHA-256 pinned in
    ``package.json``'s ``remote_binary`` manifest.

    Attributes:
        name: tool name (``"legendary"``).
        search_paths: ordered list of candidate paths
            (relative to the plugin root or absolute).
    """

    name: str
    search_paths: list[str] = field(default_factory=list)
    # No ``version_flag`` / ``min_version``. Both fed ``check_version``,
    # which had zero callers and is gone; ``min_version`` was never compared
    # for any store. SHA-256 pinning in package.json is the real guarantee.
    # Audit register item 44.

@dataclass
class SyncRequest:
    """Queued request shape consumed by :meth:`SyncService._enqueue`.

    The request queue is what makes auth-chained syncs work: when a
    store finishes login while another sync is in flight, we don't
    drop the post-auth refresh — we queue it and run it as soon as
    the lock releases.

    Attributes:
        kind: ``"sync"`` or ``"force"``. When two requests merge,
            ``"force"`` wins (a force-sync semantically supersedes
            a normal sync).
        source: provenance — ``"manual"`` | ``"auth:<store>"`` |
            ``"background"`` | ``"scheduled"``. Surfaced in logs and
            in the response so the frontend can distinguish
            user-initiated from auto-triggered syncs.
        fetch_artwork: forwarded to ``SyncService.sync_all``. When
            two requests merge, OR-ed so the wider preference wins
            (one wants artwork → result wants artwork).
        resync_artwork: forwarded to ``SyncService.sync_all``.
            OR-ed on merge for the same reason.
    """

    kind: str = "sync"
    source: str = "manual"
    fetch_artwork: bool = True
    resync_artwork: bool = False

    def merge(self, other: SyncRequest) -> SyncRequest:
        """Combine two queued requests; force wins, flags OR together.

        Returns a new request so neither input is mutated — easier
        to reason about in the queue logic.
        """
        return SyncRequest(
            kind="force" if "force" in (self.kind, other.kind) else "sync",
            source=other.source or self.source,
            fetch_artwork=self.fetch_artwork or other.fetch_artwork,
            resync_artwork=self.resync_artwork or other.resync_artwork,
        )
