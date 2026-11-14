"""The shared wrapper-store install watcher.

A wrapper store's install is a user click inside a vendor Windows client that
we neither run nor can measure. All the backend can do is watch the prefix and
decide, from the outside, when the game is there and when the attempt has been
abandoned. Getting that wrong is expensive in both directions: end too early
and a game shows a Play button with no files behind it (which is exactly what
Battle.net used to do, by not watching at all); end too late and an install
that will never finish sits on "Follow the launcher window" for two hours.

The loop is driven with a fake probe and a scripted liveness answer, so these
pin the decisions rather than any store's detection.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from unifideck.stores.shared.wrapper_install import watch as mod


class _Probe:
    """A scripted probe: yields a dir after N polls, completes after M more."""

    store = "battlenet"
    client_label = "Test Client"
    poll_interval_s = 0.001
    timeout_s = 1.0
    never_started_grace_s = 0.005
    client_gone_grace_s = 0.01

    def __init__(
        self,
        *,
        detect_after: int = 1,
        complete_after: int = 1,
        verdict: bool | None = True,
        sizes: list[int] | None = None,
    ) -> None:
        self.detect_after = detect_after
        self.complete_after = complete_after
        self._verdict = verdict
        self._sizes = sizes or []
        self.detect_calls = 0
        self.complete_calls = 0
        self.snapshots = 0

    def snapshot(self) -> str:
        self.snapshots += 1
        return "baseline"

    def detect(self, baseline: Any) -> str | None:
        assert baseline == "baseline", "the baseline must reach detect()"
        self.detect_calls += 1
        return "/install/dir" if self.detect_calls >= self.detect_after else None

    def measure(self, install_dir: str) -> int:
        del install_dir
        if not self._sizes:
            return 1000
        # ``is_complete`` runs first each poll, so it has already counted this
        # one — step back to index this poll's size rather than the next.
        index = min(max(self.complete_calls - 1, 0), len(self._sizes) - 1)
        return self._sizes[index]

    def is_complete(self, install_dir: str) -> bool | None:
        del install_dir
        self.complete_calls += 1
        if self._verdict is None:
            return None
        return self._verdict and self.complete_calls >= self.complete_after


def _alive(monkeypatch: pytest.MonkeyPatch, *answers: bool) -> None:
    """Script ``install_alive``; the last answer repeats forever."""
    seq = list(answers)

    def _probe(_store: str, _prefix: Any) -> bool:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(mod, "install_alive", _probe)


def _run(probe: Any, progress: Any = None, on_ready: Any = None) -> str | None:
    return asyncio.run(
        mod.watch_manual_install(
            probe=probe, prefix="/pfx", progress_cb=progress, on_ready=on_ready,
        ),
    )


# ── the happy path ──────────────────────────────────────────────


def test_returns_the_install_dir_once_the_store_reports_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, True)
    probe = _Probe(detect_after=2, complete_after=2)

    assert _run(probe) == "/install/dir"
    assert probe.snapshots == 1


def test_the_snapshot_is_taken_before_the_client_is_asked_to_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A baseline captured after ``on_ready`` could miss a dir the client makes.

    ``on_ready`` is what triggers the frontend's ``RunGame``, so anything the
    client creates on startup would already be in a later baseline and never
    read as new.
    """
    _alive(monkeypatch, True)
    probe = _Probe()
    order: list[str] = []

    async def _ready() -> None:
        order.append(f"ready@{probe.snapshots}")

    _run(probe, on_ready=_ready)

    assert order == ["ready@1"]


def test_progress_reports_the_growing_byte_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, True)
    seen: list[str] = []

    async def _progress(payload: dict[str, Any]) -> None:
        assert payload["phase"] == "manual"
        seen.append(payload["phase_message"])

    _run(
        _Probe(detect_after=1, complete_after=3, sizes=[1024**3, 2 * 1024**3]),
        progress=_progress,
    )

    assert any("Test Client is opening" in m for m in seen)
    # The folder name, not the whole path — the message is read on a handheld.
    assert any("Installing dir via Test Client" in m for m in seen)
    assert any("1.0 GB" in m for m in seen)


# ── completion: the store's verdict beats the heuristic ─────────


def test_a_false_verdict_is_believed_over_a_steady_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused download looks exactly like a finished one to the heuristic.

    Ubisoft has to live with that; a store that can answer must not. This is
    the whole reason the verdict is three-valued.
    """
    _alive(monkeypatch, True)
    probe = _Probe(detect_after=1, complete_after=40, sizes=[500])

    _run(probe)

    assert probe.complete_calls >= 40, "an unchanging size must not end it early"


def test_a_store_with_no_verdict_falls_back_to_size_stability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, True)
    probe = _Probe(detect_after=1, verdict=None, sizes=[500])

    assert _run(probe) == "/install/dir"
    # Three consecutive equal, non-zero reads — no more, no fewer.
    assert probe.complete_calls == mod.STABILITY_THRESHOLD + 1


def test_a_zero_size_never_counts_as_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty dir holds steady at zero forever; that is not a finished game."""
    _alive(monkeypatch, True)
    probe = _Probe(detect_after=1, verdict=None, sizes=[0])

    _run(probe)

    # The full download budget, not three polls: zero never satisfies the
    # stability rule, so the wait runs to its ceiling. That ceiling now comes
    # from the probe's own timeout rather than a fixed count, so it is asked
    # for here the same way the watcher derives it.
    assert probe.complete_calls == mod._Watch(probe, "/pfx", None)._completion_polls


# ── the give-up watchdogs ───────────────────────────────────────


def test_gives_up_when_the_client_never_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, False)
    probe = _Probe(detect_after=10**6)

    assert _run(probe) is None


def test_gives_up_when_the_client_is_quit_after_being_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, True, False)
    probe = _Probe(detect_after=10**6)

    assert _run(probe) is None


def test_a_live_client_never_trips_either_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the overall timeout may end a watch while the client is working.

    Ubisoft Connect minimises to tray during long downloads and Battle.net
    hands the download to a separate ``Agent.exe``, so "no window" is routinely
    true of a perfectly healthy install.
    """
    _alive(monkeypatch, True)
    probe = _Probe(detect_after=10**6)

    assert _run(probe) is None
    # Ran to the timeout rather than bailing at the (much shorter) graces.
    assert probe.detect_calls == int(probe.timeout_s / probe.poll_interval_s)


def test_no_watchdogs_once_the_game_is_on_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A download finishes whether or not the client window is still up."""
    _alive(monkeypatch, True, False)
    probe = _Probe(detect_after=1, complete_after=20)

    assert _run(probe) == "/install/dir"


def test_an_unreadable_liveness_probe_reads_as_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken probe must never be able to abort a real install."""

    def _boom(_store: str, _prefix: Any) -> bool:
        raise OSError("no /proc")

    monkeypatch.setattr(mod, "install_active_in", _boom)
    monkeypatch.setattr(mod, "live_client_prefixes", _boom)

    assert mod.install_alive("battlenet", "/pfx") is True


def test_liveness_falls_back_to_the_client_in_any_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client whose WINEPREFIX we cannot match must not read as gone."""
    monkeypatch.setattr(mod, "install_active_in", lambda _s, _p: False)
    monkeypatch.setattr(mod, "live_client_prefixes", lambda _s: ["/elsewhere"])

    assert mod.install_alive("battlenet", "/pfx") is True


# ── cancel ──────────────────────────────────────────────────────


def test_cancel_propagates_and_the_loop_closes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the client is the caller's call — it owns the prefix's fate."""
    _alive(monkeypatch, True)
    killed: list[Any] = []
    monkeypatch.setattr(
        "unifideck.launcher.proton.handlers.wrapper_clients.kill_client",
        lambda *a, **k: killed.append(a),
    )

    async def _cancel_soon() -> None:
        task = asyncio.ensure_future(
            mod.watch_manual_install(
                probe=_Probe(detect_after=10**6),
                prefix="/pfx",
                progress_cb=None,
            ),
        )
        await asyncio.sleep(0.01)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_cancel_soon())

    assert killed == []


# ── an explained wait ───────────────────────────────────────────


class _ExplainingProbe(_Probe):
    """A probe that can say why nothing is moving. Optional on the protocol."""

    def __init__(self, message: str | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.message = message
        self.explained = 0

    def status_message(self) -> str | None:
        self.explained += 1
        return self.message


def _messages(probe: Any) -> list[str]:
    seen: list[str] = []

    async def _progress(payload: dict[str, Any]) -> None:
        seen.append(payload["phase_message"])

    _run(probe, progress=_progress)
    return seen


def test_a_store_that_can_explain_the_wait_replaces_the_generic_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Waiting for the game to install" and a real hang look identical.

    Battle.net can tell them apart: its Agent runs one exclusive operation at
    a time and logs which one holds the slot, so a 28-minute wait behind the
    Agent's own self-update gets named instead of mimed.
    """
    _alive(monkeypatch, True)
    note = "Battle.net is updating its downloader (75%). Don't cancel."

    seen = _messages(_ExplainingProbe(note, detect_after=10, complete_after=1))

    assert note in seen
    assert not any("Waiting for the game to install" in m for m in seen)


def test_the_explanation_also_beats_a_stalled_byte_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendor client makes the install directory when it *accepts* the job.

    So a game queued behind the client's own update sits at a few KB for as
    long as that takes, and "Installing… (0.0 GB)" is a worse answer than
    naming what is in front of it.
    """
    _alive(monkeypatch, True)
    note = "Battle.net is updating its downloader (12%). Don't cancel."

    seen = _messages(
        _ExplainingProbe(note, detect_after=1, complete_after=4, sizes=[24_576]),
    )

    assert note in seen
    assert not any("Installing…" in m for m in seen)


def test_a_probe_with_nothing_to_say_keeps_the_generic_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, True)

    seen = _messages(_ExplainingProbe(None, detect_after=10, complete_after=1))

    assert any("Waiting for the game to install" in m for m in seen)


def test_a_probe_without_the_method_at_all_is_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ubisoft's probe does not implement it, and must not have to."""
    _alive(monkeypatch, True)
    probe = _Probe(detect_after=10, complete_after=1)
    assert not hasattr(probe, "status_message")

    assert any(
        "Waiting for the game to install" in m for m in _messages(probe)
    )


def test_a_raising_status_message_never_ends_an_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A progress *message* must not be able to fail the thing it describes."""
    _alive(monkeypatch, True)

    class _Exploding(_Probe):
        def status_message(self) -> str | None:
            raise RuntimeError("log unreadable")

    probe = _Exploding(detect_after=2, complete_after=2)

    assert _run(probe) == "/install/dir"


# ── the download budget ─────────────────────────────────────────


def test_the_download_wait_follows_the_probes_own_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store cannot declare four hours and silently be given ninety minutes.

    The budget was a fixed 360 polls. At Battle.net's 15 second interval that
    is 90 minutes, against the 4 hours its probe declares. The clock starts
    when ``detect()`` first sees the install directory, and a vendor client
    creates that when it *accepts* the job, so a long queue wait plus a large
    download ran past the fixed budget and a healthy install was reported as
    failed.
    """
    _alive(monkeypatch, True)
    probe = _Probe()
    probe.poll_interval_s = 15.0
    probe.timeout_s = 4 * 60 * 60

    watch = mod._Watch(probe, "/pfx", None)

    assert watch._completion_polls == 960


def test_a_short_timeout_never_shrinks_the_download_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deriving the budget must not take headroom away from anyone.

    Ubisoft's probe declares no timeout, so it inherits the two-hour default
    at a 10 second poll. That is 720 polls, already above the old floor; a
    store with a genuinely short timeout keeps the floor rather than losing
    download time it had before.
    """
    _alive(monkeypatch, True)
    probe = _Probe()
    probe.poll_interval_s = 10.0
    probe.timeout_s = 60.0

    watch = mod._Watch(probe, "/pfx", None)

    assert watch._completion_polls == mod.STABILITY_MAX_POLLS


# ── closing the client ends the install at once ─────────────────


def _in_use(monkeypatch: pytest.MonkeyPatch, *answers: bool) -> None:
    """Script ``prefix_in_use``; the last answer repeats forever."""
    seq = list(answers)

    def _probe(_prefix: Any) -> bool:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(mod, "prefix_in_use", _probe)
    monkeypatch.setattr(mod, "CLOSE_CHECK_S", 0.0)  # check on every poll


def test_closing_the_client_ends_the_wait_without_the_grace_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user closed UPC: "Follow the window" must not hang on for minutes.

    The liveness probe still says alive (another client somewhere, or the
    grace period) but the prefix the client ran in is empty, which only
    happens once its whole umu chain has exited.
    """
    _alive(monkeypatch, True)
    _in_use(monkeypatch, True, True, False)
    probe = _Probe(detect_after=10_000)

    assert _run(probe) is None
    assert probe.detect_calls <= 2


def test_an_empty_prefix_before_the_client_ever_ran_is_not_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing has started yet: only the never-started watchdog may end it."""
    _alive(monkeypatch, True)
    _in_use(monkeypatch, False)

    assert _run(_Probe(detect_after=3, complete_after=1)) == "/install/dir"


def test_closing_mid_download_fails_unless_the_store_says_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alive(monkeypatch, True)
    # In use through detection, then gone during the download wait.
    _in_use(monkeypatch, True, True, False)
    assert _run(_Probe(detect_after=1, verdict=False)) is None

    _in_use(monkeypatch, True, True, False)
    assert _run(_Probe(detect_after=1, complete_after=1)) == "/install/dir"


def test_an_unreadable_proc_counts_as_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unifideck.launcher.proton.handlers import wrapper_clients as wc

    monkeypatch.setattr(wc, "pids", list)
    assert wc.prefix_in_use("/pfx") is True
