"""What a wrapper store must be able to answer about its own install.

py_modules/unifideck/stores/shared/wrapper_install/probe.py

A wrapper store cannot measure its installs: the vendor's Windows client owns
the download and reports nothing we can read as a percentage. What every one of
them *can* do is watch its prefix and recognise the game arriving. That watching
loop is identical across stores — it is the timeouts, the give-up watchdogs and
the completion rule that took incidents to get right — so it lives once, in
:mod:`.watch`. Only the recognising differs, and this is where a store says how.

Four questions, deliberately no more:

``snapshot``/``detect``
    What was in the prefix before, and has the game appeared since. Ubisoft
    diffs directory listings; Battle.net reads the client's own ``product.db``
    for the uid it asked for, which is both stronger and immune to the
    mis-attribution a directory diff can suffer when a prefix holds two games.

``measure``
    Bytes on disk so far. Feeds the "Installing… (N GB)" tick, and — only for a
    store with no better signal — the completion rule. It must count *allocated*
    blocks, not file length: both vendor clients pre-allocate the whole game up
    front, so apparent size reaches its final value within seconds of the
    download starting and then never moves again. Use
    :func:`~unifideck.stores.shared.installed_size.dir_allocated_bytes`.

``is_complete``
    The store's authoritative "the install is finished", or ``None`` when it has
    none. Both stores can answer today, by different routes: Battle.net reads
    ``product.db``, which carries ``installed``/``playable``/``update_complete``
    and flips all three in a single write; Ubisoft reads whether UPC has drained
    its ``uplay_download/`` staging directory. A store that can answer is
    believed; only one that answers ``None`` falls back to the size heuristic.

    Answering ``False`` is not a formality — it *suppresses* the heuristic for
    that poll. That is the whole reason the verdict is three-valued rather than
    a bool, and it is what keeps a mid-download pause (a chunk being verified, a
    network stall, a phase change) from reading as "done". Ubisoft used to
    return ``None`` throughout and rode the heuristic the entire way; combined
    with an apparent-size ``measure`` it declared a 2.4 GB install complete
    41 seconds in, releasing the download queue's single slot 18 minutes early.

And one optional fifth, ``status_message``, which is not about recognising
anything. It is about the wait being legible. A correct 28-minute wait and a
hang look identical from outside, and the store is the only layer that can tell
them apart: Battle.net's Agent runs one exclusive operation at a time and says
so in its own logs, so "Queued behind Battle.net updating itself (88%)" is
knowable while the generic tick can only say "waiting". Optional because a
store with nothing to add should not have to say so: the watcher reads it with
``getattr``, exactly as it reads the optional timing attributes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class InstallFacts:
    """What a finished wrapper install resolved to.

    Returned by each store's own finalisation step, not by the shared watcher:
    writing an install marker, seeding a launch id and recording paths in an id
    map are store-specific side effects, and the same reasoning that keeps
    ``holds_game`` out of :mod:`..prefix_placement` keeps them out of here.
    """

    install_path: str
    exe_path: str | None = None
    size_bytes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class InstallProbe(Protocol):
    """The per-store half of a wrapper-store manual install."""

    #: Store id — keys ``CLIENT_IMAGES`` and the wrapper-store predicates.
    store: str
    #: Vendor client's user-facing name, for progress messages.
    client_label: str

    def snapshot(self) -> Any:
        """Opaque pre-install baseline, handed back to :meth:`detect`."""
        ...

    def detect(self, baseline: Any) -> str | None:
        """Host-side install directory once the game appears, else ``None``."""
        ...

    def measure(self, install_dir: str) -> int:
        """Bytes written so far under ``install_dir``."""
        ...

    def is_complete(self, install_dir: str) -> bool | None:
        """Authoritative completion verdict, or ``None`` when there is none."""
        ...

    # Deliberately NOT declared here: ``status_message() -> str | None``, and
    # the timing overrides ``poll_interval_s`` / ``timeout_s`` /
    # ``client_gone_grace_s`` / ``never_started_grace_s``. A Protocol member is
    # required of every implementer, so declaring an optional one would oblige
    # Ubisoft's probe to carry a method it has nothing to say through. The
    # watcher reads all of them with ``getattr`` and falls back; this comment
    # is the contract.
