"""``DOWNLOAD_*`` has exactly one emitter: the DownloadWorker.

Epic and Amazon used to emit the whole download lifecycle a second time
from their installers, in a flat ``store=``/``game_id=`` shape with no
queue item attached. Both copies reached the frontend, so:

* a failed Epic/Amazon install popped **two** toasts, the store-shaped one
  first and without the game's name (it had no ``item.game_title``);
* the store-shaped completion carried no ``game``, so the frontend's
  ``extractAppId`` returned ``null`` and that event's cache invalidation
  silently no-opped;
* the store-shaped completion also fired *before* the worker ran prefix
  warmup — up to 600s early. That premature-completion shape is what broke
  Battle.net installs once (see ``launcher/wrapper_prefix_probe.py``);
* the ``download_completed`` / ``download_failed`` counters that ship in
  every support bundle read double for exactly those two stores.

``DownloadWorker`` is the sole dispatcher for all six stores'
``install_game`` / ``update_game``, so it is the only place that knows when
a download really starts and really finishes. The installers report through
their ``InstallResult`` and emit nothing.

Audit item #4. ``scripts/validate_event_schemas.py`` guards the same
invariant statically (narrow kwargs + an emitter-path allowlist); these
tests cover what a static check cannot see — that the surviving emit
carries the item, and that the installers stay silent at runtime.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from unifideck.core.types import Events
from unifideck.services.download.models import DownloadItem
from unifideck.services.download.worker import _WorkerMixin
from unifideck.stores.amazon.amazon_install import AmazonInstaller
from unifideck.stores.epic.install import EpicInstaller


class _FakeStdout:
    """Feeds canned lines, then EOF."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = [f"{line}\n".encode() for line in lines]

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess."""

    def __init__(self, lines: list[str], returncode: int) -> None:
        self.stdout = _FakeStdout(lines)
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:  # pragma: no cover - timeout path unused here
        pass


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch, procs: list[_FakeProc],
) -> None:
    """Feed ``procs`` to successive ``create_subprocess_exec`` calls.

    One entry per expected run — Epic retries once without DLC when the
    DLC-inclusive attempt fails, and each run needs its own undrained
    stdout or the retry captures no error tail.
    """
    queue = list(procs)

    async def fake_exec(*_cmd: str, **_kw: Any) -> _FakeProc:
        return queue.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def _download_events(bus: AsyncMock) -> list[str]:
    """Names of every ``DOWNLOAD_*`` event emitted on *bus*."""
    out = []
    for call in bus.emit.await_args_list:
        args, kwargs = call
        name = args[0] if args else kwargs.get("event")
        value = getattr(name, "value", name)
        if isinstance(value, str) and value.startswith("download_"):
            out.append(value)
    return out


# ── the installers emit nothing ───────────────────────────────────
@pytest.mark.asyncio
async def test_epic_installer_emits_no_download_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    """Neither the progress ticks nor the terminal failure."""
    inst = EpicInstaller.__new__(EpicInstaller)
    inst._bus = AsyncMock()
    inst._cli_path = "/opt/plugin/bin/legendary"
    inst._library = AsyncMock()
    inst._exe_resolver = AsyncMock()
    inst._default_install_root = str(tmp_path)
    inst._install_timeout = 7200
    inst._uninstall_timeout = 120
    lines = [
        "Progress: 50.5% (1/2), Running for 00:00:10, ETA: 00:00:10",
        "[cli] ERROR: No app asset found for platform Windows",
    ]
    # Two runs: the DLC attempt, then the no-DLC retry.
    _patch_subprocess(
        monkeypatch,
        [_FakeProc(lines, returncode=1), _FakeProc(lines, returncode=1)],
    )

    result = await inst.install_game("g", base_path=str(tmp_path))

    assert not result.success
    assert _download_events(inst._bus) == []
    # The error still reaches the caller — it is the worker's toast text.
    assert "No app asset found" in result.error


@pytest.mark.asyncio
async def test_amazon_installer_emits_no_download_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    inst = AmazonInstaller(
        bus=AsyncMock(),
        cli_path="/opt/plugin/bin/nile",
        library=AsyncMock(),
        find_exe=lambda _p, _h: None,
        default_install_root=str(tmp_path),
    )
    _patch_subprocess(
        monkeypatch,
        [_FakeProc(
            [
                "= Progress: 42.50 100/200, Running for: 00:00:10, ETA: 00:00:10",
                "ERROR: Unable to fetch game manifest",
            ],
            returncode=1,
        )],
    )

    result = await inst.install_game("amzn1.adg.product.x", base_path=str(tmp_path))

    assert not result.success
    assert _download_events(inst._bus) == []
    assert "Unable to fetch game manifest" in result.error


# ── the surviving emit carries the item ───────────────────────────
class _Worker(_WorkerMixin):
    """The progress half of the worker, standalone."""

    def __init__(self, bus: Any) -> None:
        self._bus = bus


def _emit_kwargs(bus: AsyncMock, event: Events) -> dict[str, Any]:
    """The kwargs of the single emit of *event*."""
    matching = [
        call.kwargs for call in bus.emit.await_args_list
        if call.args and call.args[0] == event
    ]
    assert len(matching) == 1, f"expected 1 {event}, got {len(matching)}"
    return matching[0]


@pytest.mark.asyncio
async def test_worker_progress_carries_the_item_not_flat_fields() -> None:
    """One shape for the whole family, keyed so the UI can match a row.

    The flat ``store``/``game_id``/``progress`` payload had no row id, so
    the frontend applied every tick to whichever download was on screen.
    """
    bus = AsyncMock()
    worker = _Worker(bus)
    item = DownloadItem(
        store="gog", game_id="g1", install_path="/games/g1", title="A Game",
    )

    await worker._update_progress(item, {"percentage": 42.0, "speed_bps": 2097152.0})

    kwargs = _emit_kwargs(bus, Events.DOWNLOAD_PROGRESS)
    assert set(kwargs) == {"item"}
    emitted = kwargs["item"]
    # The id the frontend matches the visible row on.
    assert emitted["id"] == "gog:g1"
    assert emitted["progress_percent"] == 42.0
    assert emitted["speed_mbps"] == 2.0
    # Phase and title ride along, so the row no longer waits for a refetch.
    assert emitted["game_title"] == "A Game"
    assert emitted["download_phase"] == "downloading"


@pytest.mark.asyncio
async def test_worker_progress_is_emitted_for_a_bare_float_too() -> None:
    """Epic/Amazon pass a float or a partial dict; both emit the item."""
    bus = AsyncMock()
    worker = _Worker(bus)
    item = DownloadItem(store="epic", game_id="g1", install_path="/games/g1")

    await worker._update_progress(item, 77.5)

    emitted = _emit_kwargs(bus, Events.DOWNLOAD_PROGRESS)["item"]
    assert emitted["id"] == "epic:g1"
    assert emitted["progress_percent"] == 77.5
