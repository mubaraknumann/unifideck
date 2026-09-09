"""The cloud-only store refuses installs instead of faking them.

Audit §3.5 bullet 1 / register item 11. ``install_game``,
``uninstall_game`` and ``update_game`` returned ``success=True`` and did
nothing, which tells the caller a game was installed (or uninstalled) that
never was.

Install and update were unreachable — the play section short-circuits any
``xcloud``-tagged game before the Install button mounts, and the download
worker carried its own store-name rejection. ``uninstall_game`` had no
backend guard at all, only the frontend's ``is_installed`` gate, so it is
the one a caller could actually reach.

``store_info.supports_install`` is deliberately *not* asserted as the
guard: it has no readers anywhere (register item 26), so it gates nothing.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from unifideck.services.download import worker as worker_mod
from unifideck.stores.microsoft.microsoft_store import MicrosoftStore


class _Bus:
    async def emit(self, *_a: Any, **_k: Any) -> None:
        return None

    def on(self, *_a: Any, **_k: Any) -> None:
        return None


class _Cache:
    def get(self, *_a: Any, **_k: Any) -> None:
        return None

    def register(self, *_a: Any, **_k: Any) -> None:
        return None


@pytest.fixture
def store() -> MicrosoftStore:
    return MicrosoftStore(_Bus(), _Cache(), plugin_dir="/plugin")


def test_install_is_refused(store: MicrosoftStore) -> None:
    result = asyncio.run(store.install_game("9NXR0000TEST"))
    assert result.success is False
    assert result.error == "not_supported"
    assert result.install_path is None


def test_uninstall_is_refused(store: MicrosoftStore) -> None:
    """The one of the three a caller could actually reach.

    ``success=True`` here let a shortcut flip out of an installed state
    it never held, and told the RPC caller a deletion had happened.
    """
    result = asyncio.run(store.uninstall_game("9NXR0000TEST"))
    assert result.success is False
    assert result.error == "not_supported"


def test_update_is_refused(store: MicrosoftStore) -> None:
    result = asyncio.run(store.update_game("9NXR0000TEST"))
    assert result.success is False
    assert result.error == "not_supported"


def test_refusals_carry_a_machine_readable_code(store: MicrosoftStore) -> None:
    """``error`` is for humans, ``error_code`` is for dispatch.

    ``Result.error_code``'s own docstring says callers must branch on it
    rather than string-matching ``error``, so a refusal that populates
    only ``error`` forces exactly the fragile matching it warns against.
    """
    for coro in (
        store.install_game("x"),
        store.uninstall_game("x"),
        store.update_game("x"),
    ):
        assert asyncio.run(coro).error_code == "not_supported"


def test_worker_has_no_store_name_rejection_left() -> None:
    """The store's refusal replaced the worker's special case.

    The worker's version bypassed ``_emit_failure``, so the queue row
    never reached "failed" and never got an ``error`` — while the toast
    echoed a hardcoded English sentence straight past
    ``friendlyDownloadError``'s code table. Two guards for one rule also
    meant either could drift; this pins that only one remains.
    """
    assert not hasattr(worker_mod._WorkerMixin, "_reject_microsoft")
    # Comments are stripped first: the note left in place of the deleted
    # branch names it, and a test that reads comments would fail on the
    # explanation rather than on a branch.
    code = "\n".join(
        line.split("#", 1)[0]
        for line in inspect.getsource(
            worker_mod._WorkerMixin._execute_install,
        ).splitlines()
    )
    assert "microsoft" not in code.lower()
