"""Unit tests for ``run_umu_with_retry``'s recoverable-code retry branch.

Regression (UD-022): a GOG game launched through forced compatibility
returned an umu recoverable exit code (2 / 74 / 127) and the plugin
showed a toast titled "Network Error" while wiping the *shared*
steamrt runtime cache (hundreds of MB, re-downloaded on the next
launch of any game) on EVERY recoverable code. The title was a
misdiagnosis (127 = command-not-found, 2/74 = umu bootstrap failure —
none are network errors) and the blanket cache nuke was wasteful.

These tests pin the fixed semantics:
  * the retry toast title is now ``toasts.header.gameSetup``, never
    ``networkError``;
  * the shared runtime cache is wiped ONLY for the corruption codes
    (2, 74), NOT for 127;
  * a non-recoverable code returns immediately without a toast or wipe.

The retry loop is exercised without a real umu by scripting
``_run_umu_once`` to return a sequence of exit codes, and the 3s
backoff is neutered so the suite stays sub-second.
"""
from __future__ import annotations

import pytest

from unifideck.launcher.proton.infrastructure import umu_runtime as ur


@pytest.fixture()
def retry_harness(monkeypatch):
    """Script ``_run_umu_once`` and spy on the toast + cache-wipe.

    Returns a small object with ``toasts`` (list of captured toast
    kwargs), ``wipes`` (call count of ``cleanup_umu_runtime_cache``),
    and ``once_calls`` (how many times ``_run_umu_once`` ran).
    """
    state = {"codes": [], "toasts": [], "wipes": 0, "once_calls": 0}

    async def _scripted(*_args, **_kwargs):
        state["once_calls"] += 1
        return state["codes"].pop(0)

    def _toast(i18n_key, **kwargs):
        state["toasts"].append({"i18n_key": i18n_key, **kwargs})

    def _wipe():
        state["wipes"] += 1

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(ur, "_run_umu_once", _scripted)
    monkeypatch.setattr(ur, "launcher_toast", _toast)
    monkeypatch.setattr(ur, "cleanup_umu_runtime_cache", _wipe)
    monkeypatch.setattr(ur.asyncio, "sleep", _no_sleep)

    class _H:
        toasts = state["toasts"]

        @staticmethod
        def script(codes):
            state["codes"] = list(codes)

        @staticmethod
        def wipes():
            return state["wipes"]

        @staticmethod
        def once_calls():
            return state["once_calls"]

    return _H


async def test_code_127_retries_without_cache_wipe(retry_harness):
    """127 (command-not-found) is recoverable but must NOT nuke the cache."""
    retry_harness.script([127, 0])

    rc = await ur.run_umu_with_retry(["umu"], max_attempts=2)

    assert rc == 0
    assert retry_harness.once_calls() == 2
    assert retry_harness.wipes() == 0
    assert len(retry_harness.toasts) == 1
    assert retry_harness.toasts[0]["i18n_title_key"] == "toasts.header.gameSetup"
    # Guard against the old misleading title regressing.
    assert retry_harness.toasts[0]["i18n_title_key"] != "toasts.launcher.networkError"


async def test_code_2_retries_and_wipes_cache(retry_harness):
    """2 (umu bootstrap failure) still wipes the shared runtime cache."""
    retry_harness.script([2, 0])

    rc = await ur.run_umu_with_retry(["umu"], max_attempts=2)

    assert rc == 0
    assert retry_harness.wipes() == 1


async def test_code_74_wipes_cache(retry_harness):
    """74 (umu I/O/setup failure) is also treated as runtime corruption."""
    retry_harness.script([74, 0])

    rc = await ur.run_umu_with_retry(["umu"], max_attempts=2)

    assert rc == 0
    assert retry_harness.wipes() == 1


async def test_exhausted_retries_returns_last_rc(retry_harness):
    """A code that never recovers returns the code; toast fires once."""
    retry_harness.script([2, 2])

    rc = await ur.run_umu_with_retry(["umu"], max_attempts=2)

    assert rc == 2
    # The toast only fires before a further attempt (attempt < max), so
    # the final failing attempt does not add a second toast.
    assert len(retry_harness.toasts) == 1
    assert retry_harness.wipes() == 1
    assert retry_harness.once_calls() == 2


async def test_toast_reports_attempt_and_max(retry_harness):
    """The toast body params name the next attempt and the max honestly."""
    retry_harness.script([127, 0])

    await ur.run_umu_with_retry(["umu"], max_attempts=2)

    toast = retry_harness.toasts[0]
    assert toast["i18n_key"] == "toasts.launcher.retryingUmu"
    assert toast["i18n_params"] == {
        "seconds": ur._RETRY_BACKOFF_SECONDS,
        "attempt": 2,
        "max": 2,
    }
    assert toast["severity"] == "warning"


async def test_nonrecoverable_code_no_toast_no_wipe(retry_harness):
    """A plain game crash (rc=1) returns at once — no "Retrying" toast."""
    retry_harness.script([1])

    rc = await ur.run_umu_with_retry(["umu"], max_attempts=2)

    assert rc == 1
    assert retry_harness.once_calls() == 1
    assert retry_harness.toasts == []
    assert retry_harness.wipes() == 0


async def test_success_first_attempt_no_toast(retry_harness):
    """rc=0 on the first attempt returns 0 with no retry side effects."""
    retry_harness.script([0])

    rc = await ur.run_umu_with_retry(["umu"], max_attempts=2)

    assert rc == 0
    assert retry_harness.once_calls() == 1
    assert retry_harness.toasts == []
    assert retry_harness.wipes() == 0


# ── UD-126: a recoverable code is only "recoverable" when it arrives fast ──
#
# Epic used to report *legendary's* exit code (always 0 — legendary
# fire-and-forgot the game), so this loop never saw a real game's status.
# Now that the launcher owns umu-run for every store, a title whose normal
# quit code happens to be 2/74/127 would otherwise relaunch itself after
# the user quit — and, for 2/74, wipe the shared runtime every session.


@pytest.fixture()
def slow_attempt(monkeypatch):
    """Make every attempt look like it ran past the recoverable window."""
    ticks = iter(range(0, 10_000, ur._RECOVERABLE_MAX_RUNTIME_SECONDS + 1))
    monkeypatch.setattr(ur, "_now", lambda: float(next(ticks)))


async def test_long_session_recoverable_code_is_not_retried(
    retry_harness, slow_attempt,
):
    """A game that ran for ages and exits 74 is a quit, not a bad runtime."""
    retry_harness.script([74, 0])

    rc = await ur.run_umu_with_retry(["umu"], max_attempts=2)

    assert rc == 74
    assert retry_harness.once_calls() == 1
    assert retry_harness.wipes() == 0, "must not nuke the shared runtime cache"
    assert retry_harness.toasts == [], "no 'Retrying Launch' toast after a session"


async def test_long_session_127_is_not_retried(retry_harness, slow_attempt):
    """Same for 127 — long-running means the command was clearly found."""
    retry_harness.script([127, 0])

    rc = await ur.run_umu_with_retry(["umu"], max_attempts=2)

    assert rc == 127
    assert retry_harness.once_calls() == 1


async def test_fast_failure_still_retries(retry_harness):
    """The guard must not disarm the real case: a fast 2 still retries."""
    retry_harness.script([2, 0])

    rc = await ur.run_umu_with_retry(["umu"], max_attempts=2)

    assert rc == 0
    assert retry_harness.once_calls() == 2
    assert retry_harness.wipes() == 1
