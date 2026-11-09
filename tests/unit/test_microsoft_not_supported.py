"""A Microsoft title with nothing to install is refused, never faked.

Audit §3.5 bullet 1 / register item 11. ``install_game``,
``uninstall_game`` and ``update_game`` used to return ``success=True`` and
do nothing, which tells the caller a game was installed (or uninstalled)
that never was.

Xbox PC titles now install through Xodus (``xodus/store_hooks``), so the
store answers ``supports_install=True``. A stream-only title still has
nothing on disk: no Xbox PC facts from the library, no install record and
no marker. Each hook refuses it with ``not_supported``.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

from unifideck.services.download import worker as worker_mod
from unifideck.stores.microsoft.microsoft_store import MicrosoftStore


class _Bus:
    def __init__(self) -> None:
        self.emitted: list[Any] = []

    async def emit(self, *a: Any, **_k: Any) -> None:
        self.emitted.append(a[0] if a else None)

    def on(self, *_a: Any, **_k: Any) -> None:
        return None


class _Cache:
    def get(self, *_a: Any, **_k: Any) -> None:
        return None

    def set(self, *_a: Any, **_k: Any) -> None:
        return None

    def register(self, *_a: Any, **_k: Any) -> None:
        return None


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MicrosoftStore:
    # The record and marker lookups read the Xbox PC data root: keep it here.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(
        "unifideck.core.marker_sweep.collect_install_roots", lambda *a, **k: [],
    )
    return MicrosoftStore(_Bus(), _Cache(), plugin_dir="/plugin")


def test_the_store_declares_installs() -> None:
    assert MicrosoftStore.store_info.supports_install is True


def test_install_of_a_stream_only_title_is_refused(store: MicrosoftStore) -> None:
    result = asyncio.run(store.install_game("9NXR0000TEST", base_path="/games"))
    assert result.success is False
    assert result.error_code == "not_supported"
    assert result.install_path is None


def test_precondition_names_the_refusal(store: MicrosoftStore) -> None:
    """The install RPC asks first, so the frontend gets the code at once."""
    assert asyncio.run(store.install_precondition("9NXR0000TEST")) == "not_supported"


def test_uninstall_without_a_record_or_marker_is_refused(store: MicrosoftStore) -> None:
    """The one a caller could always reach.

    ``success=True`` here let a shortcut flip out of an installed state
    it never held, and told the RPC caller a deletion had happened.
    """
    result = asyncio.run(store.uninstall_game("9NXR0000TEST"))
    assert result.success is False
    assert result.error_code == "not_supported"


def test_update_with_the_feature_off_is_refused(store: MicrosoftStore) -> None:
    result = asyncio.run(store.update_game("9NXR0000TEST"))
    assert result.success is False
    assert result.error_code == "xbox_pc_disabled"


def test_stream_only_title_has_no_size_path_or_exe(store: MicrosoftStore) -> None:
    assert asyncio.run(store.get_game_size("9NXR0000TEST")) is None
    assert asyncio.run(store.get_installed_path("9NXR0000TEST")) is None
    assert asyncio.run(store.find_installed_exe("/games/x", "9NXR0000TEST")) is None


def test_no_update_check_with_the_feature_off(store: MicrosoftStore) -> None:
    assert asyncio.run(store.check_for_updates()) == []


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
