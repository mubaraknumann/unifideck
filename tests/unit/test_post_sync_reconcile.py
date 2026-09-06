"""The boot pass must repair an interrupted chain — and touch nothing else.

The post-sync chain runs as background tasks in the plugin process, and
that process restarts independently of Steam — most often right after a
sync, because that is exactly when the user is told to restart Steam so
new shortcuts and artwork load. Nothing at boot ever checked whether the
library's artwork, metadata or compat data were complete, so an
interrupted chain was simply lost until the next manual sync.

Measured 2026-08-29: the plugin unloaded at 02:23:21 with two artwork
batches in flight. The next boot ran ``orphan-scan`` (shortcuts only) and
the size-backfill resume, and nothing else. 111 of 1242 games were left
with incomplete artwork; all 13 Ubisoft titles — the store signed into
last — had none at all.

The second half of these tests is the safety half. The pass must **never**
write shortcuts.vdf: shortcut sweeping is gated on ``_sweepable_stores``,
which asks whether a store answered a sync *this run*. At boot no store
has answered anything, so there is no honest sweepable set, and a
boot-time writer would reopen the hole that once deleted a signed-out
store's entire library (audit §3.5, finding B).
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.services import post_sync_reconcile as psr


class _Bus:
    def __init__(self, syncing: bool = False) -> None:
        self.emitted: list[dict[str, Any]] = []
        self._syncing = syncing

    def get_sync_progress(self) -> Any:
        return object() if self._syncing else None

    async def emit(self, event: str, **payload: Any) -> None:
        self.emitted.append({"event": event, **payload})


class _Game:
    def __init__(self, title: str, store: str = "ubisoft") -> None:
        self.title = title
        self.store = store
        self.store_game_id = title
        self.app_id = abs(hash(title)) % 10_000 or 1
        self.metadata: dict[str, Any] = {}


class _Sync:
    def __init__(self, games: list[_Game]) -> None:
        self._games = games

    def get_all_games(self) -> list[_Game]:
        return list(self._games)


class _Artwork:
    """Reports every game as missing all five kinds, then fills them."""

    def __init__(self, missing: dict[int, set[str]]) -> None:
        self.grid_dir = "/grid"
        self._missing = missing
        self.fetched: list[tuple[str, frozenset[str]]] = []

    async def fetch_artwork(
        self, app_id: int, store: str, game_id: str, title: str,
        extras: Any = None, only_kinds: set[str] | None = None,
        force: bool = False,
    ) -> dict[str, bool]:
        self.fetched.append((title, frozenset(only_kinds or ())))
        self._missing[app_id] = set()          # repaired on "disk"
        return dict.fromkeys(only_kinds or (), True)


class _Metadata:
    def __init__(self, incomplete: set[str]) -> None:
        self._incomplete = incomplete
        self.enriched: list[str] = []

    def _has_complete_metadata(self, game: Any) -> bool:
        return game.title not in self._incomplete

    async def enrich(self, game: Any) -> dict[str, Any]:
        self.enriched.append(game.title)
        return {}


class _Compat:
    def __init__(self, pending: list[_Game]) -> None:
        self._pending = pending
        self.repaired: list[str] = []

    def _partition_games(self, games: list[Any]) -> tuple[list[Any], list[Any]]:
        pend = [g for g in games if g in self._pending]
        return [g for g in games if g not in pend], pend

    async def repair_missing(self, games: list[Any]) -> int:
        self.repaired.extend(g.title for g in games)
        return len(games)


@pytest.fixture(autouse=True)
def _patch_gap_probe(monkeypatch):
    """Route ``get_missing_kinds`` at the fake on-disk state."""
    state: dict[int, set[str]] = {}

    async def _fake(grid_dir: str, app_id: int) -> set[str]:
        return set(state.get(app_id, set()))

    import unifideck.services.artwork.fetcher as fetcher
    monkeypatch.setattr(fetcher, "get_missing_kinds", _fake)
    return state


@pytest.fixture(autouse=True)
def _network_is_up(monkeypatch):
    async def _reachable() -> bool:
        return True
    monkeypatch.setattr(
        psr.PostSyncReconcileService, "_network_reachable",
        staticmethod(_reachable),
    )


ALL_KINDS = {"grid", "grid_l", "hero", "logo", "icon"}


@pytest.mark.asyncio
async def test_repairs_the_ubisoft_shaped_gap(_patch_gap_probe):
    """The exact residue the audit found: 13 games with zero artwork."""
    games = [_Game(f"ubi{i}") for i in range(13)]
    for g in games:
        _patch_gap_probe[g.app_id] = set(ALL_KINDS)

    artwork = _Artwork(_patch_gap_probe)
    svc = psr.PostSyncReconcileService(
        _Bus(), _Sync(games), artwork=artwork,
    )
    report = await svc.run(reason="boot")

    assert report.ran is True
    assert report.artwork_gaps == 13
    assert report.artwork_repaired == 13
    assert len(artwork.fetched) == 13
    # Only the missing kinds are requested — never a blanket refetch.
    assert all(kinds == frozenset(ALL_KINDS) for _t, kinds in artwork.fetched)
    assert report.artwork_remaining == []


@pytest.mark.asyncio
async def test_only_the_missing_kinds_are_requested(_patch_gap_probe):
    """A game short one icon must not re-download the other four kinds."""
    game = _Game("partial")
    _patch_gap_probe[game.app_id] = {"icon"}
    artwork = _Artwork(_patch_gap_probe)
    svc = psr.PostSyncReconcileService(_Bus(), _Sync([game]), artwork=artwork)

    await svc.run()

    assert artwork.fetched == [("partial", frozenset({"icon"}))]


@pytest.mark.asyncio
async def test_a_complete_library_does_nothing_and_stays_silent(
    _patch_gap_probe,
):
    games = [_Game("done")]
    artwork = _Artwork(_patch_gap_probe)
    bus = _Bus()
    svc = psr.PostSyncReconcileService(bus, _Sync(games), artwork=artwork)

    report = await svc.run()

    assert report.artwork_gaps == 0
    assert artwork.fetched == []
    assert bus.emitted == [], "nothing repaired means nothing to announce"


@pytest.mark.asyncio
async def test_metadata_and_compat_gaps_are_repaired(_patch_gap_probe):
    games = [_Game("a"), _Game("b"), _Game("c")]
    metadata = _Metadata(incomplete={"a", "b"})
    compat = _Compat(pending=[games[2]])
    svc = psr.PostSyncReconcileService(
        _Bus(), _Sync(games), metadata=metadata, compat=compat,
    )

    report = await svc.run()

    assert report.metadata_gaps == 2
    assert sorted(metadata.enriched) == ["a", "b"]
    assert report.compat_gaps == 1
    assert compat.repaired == ["c"]


@pytest.mark.asyncio
async def test_toast_summarises_only_what_was_repaired(_patch_gap_probe):
    game = _Game("gap")
    _patch_gap_probe[game.app_id] = {"logo"}
    bus = _Bus()
    svc = psr.PostSyncReconcileService(
        bus, _Sync([game]), artwork=_Artwork(_patch_gap_probe),
    )

    await svc.run()

    toasts = [e for e in bus.emitted if e["event"] == "launcher_stage"]
    assert len(toasts) == 1
    assert toasts[0]["i18n_key"] == "reconcile.repairedBody"
    assert toasts[0]["i18n_params"]["artwork"] == 1


# ── the safety half ─────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_while_a_sync_is_in_flight(_patch_gap_probe):
    """The running chain owns these caches; two writers would race."""
    game = _Game("x")
    _patch_gap_probe[game.app_id] = {"icon"}
    artwork = _Artwork(_patch_gap_probe)
    svc = psr.PostSyncReconcileService(
        _Bus(syncing=True), _Sync([game]), artwork=artwork,
    )

    report = await svc.run()

    assert report.ran is False
    assert report.skipped_reason == "a sync is in flight"
    assert artwork.fetched == []


@pytest.mark.asyncio
async def test_skips_when_offline(monkeypatch, _patch_gap_probe):
    """Boot often has no DNS yet; burning the pass would look like success."""
    async def _down() -> bool:
        return False
    monkeypatch.setattr(
        psr.PostSyncReconcileService, "_network_reachable",
        staticmethod(_down),
    )
    game = _Game("x")
    _patch_gap_probe[game.app_id] = {"icon"}
    artwork = _Artwork(_patch_gap_probe)
    svc = psr.PostSyncReconcileService(_Bus(), _Sync([game]), artwork=artwork)

    report = await svc.run()

    assert report.ran is False
    assert report.skipped_reason == "network unreachable"
    assert artwork.fetched == []


@pytest.mark.asyncio
async def test_never_touches_shortcuts(_patch_gap_probe):
    """No shortcut/VDF surface may be reachable from this service.

    At boot no store has answered a sync, so ``_sweepable_stores`` has no
    honest answer and any sweep would be guessing with the user's library.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(psr))
    # Names actually referenced by code — docstrings and comments explain
    # *why* the module stays away from shortcuts, so a substring scan over
    # the raw source would match its own explanation.
    referenced: set[str] = set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            referenced.update(a.name for a in node.names)

    forbidden = {
        "write_vdf", "read_vdf", "vdf_write_lock", "_save_all",
        "write_shortcuts", "reconcile", "ShortcutService", "save_registry",
    }
    assert not (referenced & forbidden), (
        f"post_sync_reconcile must not reach shortcut machinery: "
        f"{sorted(referenced & forbidden)}"
    )
    assert not any("shortcut" in m for m in imported), (
        f"post_sync_reconcile must not import the shortcut package: "
        f"{sorted(m for m in imported if 'shortcut' in m)}"
    )


@pytest.mark.asyncio
async def test_empty_library_is_a_no_op(_patch_gap_probe):
    svc = psr.PostSyncReconcileService(_Bus(), _Sync([]))
    report = await svc.run()
    assert report.ran is False
    assert report.skipped_reason == "empty library"


@pytest.mark.asyncio
async def test_missing_collaborators_are_tolerated(_patch_gap_probe):
    """A partial bootstrap drops that component, it does not crash."""
    svc = psr.PostSyncReconcileService(_Bus(), _Sync([_Game("a")]))
    report = await svc.run()
    assert report.ran is True
    assert report.gap_total == 0


@pytest.mark.asyncio
async def test_still_incomplete_games_are_reported(_patch_gap_probe):
    """A repair that did not take must be visible, not silently 'done'."""
    game = _Game("stubborn")
    _patch_gap_probe[game.app_id] = {"logo"}

    class _Failing(_Artwork):
        async def fetch_artwork(self, *a: Any, **k: Any) -> dict[str, bool]:
            return {}          # leaves the gap on disk

    svc = psr.PostSyncReconcileService(
        _Bus(), _Sync([game]), artwork=_Failing(_patch_gap_probe),
    )
    report = await svc.run()

    assert report.artwork_gaps == 1
    assert report.artwork_repaired == 0
    assert report.artwork_remaining == ["stubborn"]
