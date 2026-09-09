"""First Battle.net sign-in must measure the WSI abort, not wait for a marker.

GitHub #446, reproduced on an ASUS ROG Xbox Ally X. The client path
(``_start_client_here``) has always read the game log after a failed start,
recorded the host marker and retried with gamescope's Vulkan WSI layer off.
``battlenet_auth_launch`` did not, and on an affected GPU that is a loop with
no exit inside the product::

    first sign-in -> no marker -> ANGLE aborts -> nothing reads the log
                  -> marker never written -> no window, ever

The reporter escaped it by hand-writing the marker file, which is both the
confirmation and the reason this needed fixing.

Two properties are load-bearing and each has a test below:

* **rc 0 must still be measured.** The abort kills the Wine session while umu
  reports success, and ``run_umu_with_retry`` returns on ``rc == 0`` before any
  retry logic runs. A fix that lives inside that function cannot see this.
* **A window the user closed must not reopen.** That was the previous bug in
  this same code path (``test_battlenet_auth_relaunch``), so the readiness
  latch — not the exit code — decides.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.launcher.proton.handlers import battlenet_auth_wsi as auth_wsi
from unifideck.launcher.proton.handlers import battlenet_wsi as wsi
from unifideck.launcher.proton.infrastructure import game_log as game_log_mod

# The real tail of the ROG Ally X log, trimmed. The layer banner, the vkroots
# frame and the assertion together are what make this a measurement.
_CRASH_LOG = """\
[Gamescope WSI] Application info:
  pApplicationName: Battle.net.exe
  pEngineName: ANGLE
[Gamescope WSI] Forcing on VK_EXT_swapchain_maintenance1.
../subprojects/vkroots/vkroots.h:129: insert(Object, DispatchPtr) \
[with Object = VkQueue_T*]: Assertion `obj' failed.
"""

_CLEAN_LOG = "[umu] launching\nwine: process exited\n"


class _Latch:
    """Stand-in for ``battlenet_watch.ReadinessLatch``."""

    def __init__(self, seen: bool) -> None:
        self.seen = seen


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the developer's real marker."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))


@pytest.fixture
def plan(tmp_path: Path) -> Any:
    return SimpleNamespace(
        env={},
        prefix_path=tmp_path / "prefix",
        on_process_start=None,
    )


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    codes: list[int],
    seen: bool,
    log_body: str,
) -> dict[str, Any]:
    """Script the umu runs and the log the client is deemed to have written."""
    record: dict[str, Any] = {"runs": [], "cleared": 0}

    async def _run(_argv: list[str], **kwargs: Any) -> int:
        record["runs"].append(dict(kwargs.get("env") or {}))
        return codes[len(record["runs"]) - 1]

    @contextlib.asynccontextmanager
    async def _readiness(*_a: Any, **_kw: Any) -> AsyncGenerator[_Latch]:
        yield _Latch(seen)

    @contextlib.asynccontextmanager
    async def _teardown(*_a: Any, **_kw: Any) -> AsyncGenerator[None]:
        yield

    async def _clear_stale(*_a: Any, **_kw: Any) -> None:
        record["cleared"] += 1

    log = tmp_path / "launch.game.log"
    log.write_text(log_body, encoding="utf-8")

    monkeypatch.setattr(auth_wsi, "run_umu_with_retry", _run)
    monkeypatch.setattr(auth_wsi.watch, "watch_readiness", _readiness)
    monkeypatch.setattr(game_log_mod, "game_log_path", lambda: log)
    record["teardown"] = _teardown
    record["clear_stale"] = _clear_stale
    return record


def _run_auth(plan: Any, record: dict[str, Any]) -> int:
    return asyncio.run(
        auth_wsi.run_auth_client(
            plan, ["/bin/true"],
            teardown=record["teardown"], clear_stale=record["clear_stale"],
        ),
    )


# --------------------------------------------------------------------------
# the bug: a first sign-in on an affected host
# --------------------------------------------------------------------------


def test_a_first_signin_measures_the_abort_and_retries_without_the_layer(
    plan: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole of #446: no marker, rc 0, no renderer, crash in the log.

    Before the fix this returned after one run and wrote nothing, so the next
    sign-in started from the same empty state.
    """
    assert wsi.workaround_recorded() is False
    record = _install_fakes(
        monkeypatch, tmp_path, codes=[0, 0], seen=False, log_body=_CRASH_LOG,
    )

    _run_auth(plan, record)

    assert len(record["runs"]) == 2, "the sign-in client must be retried once"
    assert record["runs"][0].get(wsi.DISABLE_VAR) is None
    assert record["runs"][1][wsi.DISABLE_VAR] == "1"
    assert record["cleared"] == 1, "the dead Wine session must be cleared first"
    assert wsi.workaround_recorded() is True, (
        "the marker is the point: every later launch reads it up front"
    )


def test_rc_zero_is_not_treated_as_success(
    plan: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The abort returns 0, which is why this cannot live in run_umu_with_retry."""
    record = _install_fakes(
        monkeypatch, tmp_path, codes=[0, 0], seen=False, log_body=_CRASH_LOG,
    )
    _run_auth(plan, record)
    assert len(record["runs"]) == 2


# --------------------------------------------------------------------------
# a healthy host, and a user who closed the window, pay nothing
# --------------------------------------------------------------------------


def test_a_window_the_user_closed_is_not_reopened(
    plan: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression guard for the *previous* bug in this path.

    The renderer was seen, so the exit was a close. Even with the crash text
    sitting in the log — a stale tail from an earlier run — nothing reopens.
    """
    record = _install_fakes(
        monkeypatch, tmp_path, codes=[2, 2], seen=True, log_body=_CRASH_LOG,
    )
    assert _run_auth(plan, record) == 2
    assert len(record["runs"]) == 1
    assert wsi.workaround_recorded() is False


def test_a_failure_that_is_not_the_layer_is_not_blamed_on_it(
    plan: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling the layer costs direct scanout and HDR, for the game too."""
    record = _install_fakes(
        monkeypatch, tmp_path, codes=[127, 127], seen=False, log_body=_CLEAN_LOG,
    )
    assert _run_auth(plan, record) == 127
    assert len(record["runs"]) == 1
    assert wsi.workaround_recorded() is False


def test_a_host_that_already_has_the_marker_runs_once(
    plan: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Already off and it still died, so the layer was never the problem.

    ``core._apply_battlenet_env`` puts the variable in the environment from
    the marker, and ``adopt_workaround`` refuses to re-adopt it.
    """
    wsi.record_workaround("test")
    plan.env[wsi.DISABLE_VAR] = "1"
    record = _install_fakes(
        monkeypatch, tmp_path, codes=[1, 1], seen=False, log_body=_CRASH_LOG,
    )
    assert _run_auth(plan, record) == 1
    assert len(record["runs"]) == 1
