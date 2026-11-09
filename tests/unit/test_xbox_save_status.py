"""Xbox PC save status: read-only comparison of local saves with the cloud.

The hard rule under test: nothing here may ever send anything but GET to the
save service. Only the runtime uploads, inside the game.
"""
from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from unifideck.stores.microsoft import save_status_mixin
from unifideck.stores.microsoft.tokens.endpoint import HttpReply
from unifideck.stores.microsoft.xodus import save_status as ss

XUID = "111"
SCID = "00000000-0000-0000-0000-000012345678"
T1 = "2026-11-06T14:19:45.0000000Z"
T2 = "2026-11-07T10:00:00.0000000Z"
DATA_KEY = b"Data".hex()


@pytest.fixture
def prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(ss, "canonical_prefix", lambda pid: tmp_path / pid)
    monkeypatch.setattr(ss.time, "sleep", lambda _s: None)
    return tmp_path / "GAME"


def _write(
    prefix: Path, name: str, *, remote: str = T1, dirty: int = 0, atom: str = "ATOM,binary",
    xuid: str = XUID, scid: str = SCID,
) -> Path:
    folder = prefix / ss.SAVE_FOLDER / xuid / scid / name.encode().hex()
    folder.mkdir(parents=True)
    (folder / "index").write_text(
        f"display {name.encode().hex()}\ntime 1\nremote {remote or '-'}\ndirty {dirty}\n"
        f"revision 1\nblob {DATA_KEY} 10 {atom}\n",
    )
    return folder


def _game_dir(tmp_path: Path) -> Path:
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    (game / "MicrosoftGame.config").write_text(
        '<Game><Identity Name="39EA002F.EXED1" '
        'Publisher="CN=83076AC8-252A-4004-9255-A4A444BA69F5" Version="1.0.0.0"/></Game>',
    )
    return game


class FakeService:
    """The save service. Records every request."""

    def __init__(self, containers: dict[str, str] | None = None, atoms: str = "ATOM,binary") -> None:
        self.containers = containers or {}
        self.atoms = atoms
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.listing: HttpReply | None = None

    def __call__(self, method: str, url: str, headers: dict[str, str], payload: Any = None, timeout: float = 0) -> HttpReply:
        assert payload is None
        self.calls.append((method, url, headers))
        if url.endswith(",savedgame"):
            return HttpReply(200, {"atoms": {"Data": self.atoms}})
        if self.listing is not None:
            return self.listing
        blobs = [{"fileName": f"{n},savedgame", "clientFileTime": t} for n, t in self.containers.items()]
        return HttpReply(200, {"blobs": blobs, "pagingInfo": {"continuationToken": None}})


CHAIN: Any = types.SimpleNamespace(xuid=XUID, user_hash="uhs", xsts_token="tok")  # noqa: S106


def _check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, service: FakeService, chain: Any = CHAIN) -> dict[str, Any]:
    monkeypatch.setattr(ss, "request_json", service)
    return ss.check_saves(chain, "GAME", _game_dir(tmp_path))


LOCAL_CLEAN = ss.LocalSave(remote=T1, dirty=False, blobs={DATA_KEY: "A"})
LOCAL_DIRTY = ss.LocalSave(remote=T1, dirty=True, blobs={DATA_KEY: "A"})
LOCAL_NEVER_UP = ss.LocalSave(remote="", dirty=False, blobs={})


@pytest.mark.parametrize(
    ("cloud", "local", "atoms", "expected"),
    [
        (T1, LOCAL_CLEAN, {DATA_KEY: "A"}, ss.IN_SYNC),
        (T1, LOCAL_DIRTY, None, ss.WAITING_UPLOAD),
        (T1, LOCAL_CLEAN, {DATA_KEY: "B"}, ss.UNKNOWN),
        (T1, LOCAL_CLEAN, None, ss.UNKNOWN),
        (T2, LOCAL_CLEAN, None, ss.CLOUD_NEWER),
        (T2, None, None, ss.CLOUD_NEWER),
        (T2, LOCAL_DIRTY, None, ss.BOTH_CHANGED),
        (None, LOCAL_DIRTY, None, ss.WAITING_UPLOAD),
        (None, LOCAL_NEVER_UP, None, ss.WAITING_UPLOAD),
        (None, LOCAL_CLEAN, None, ss.CLOUD_REMOVED),
    ],
)
def test_container_state_follows_the_runtime(cloud: Any, local: Any, atoms: Any, expected: str) -> None:
    assert ss.container_state(cloud, local, atoms) == expected


def test_worst_state_wins() -> None:
    order = [ss.BOTH_CHANGED, ss.CLOUD_REMOVED, ss.CLOUD_NEWER, ss.WAITING_UPLOAD, ss.UNKNOWN, ss.IN_SYNC]
    for index, state in enumerate(order):
        assert ss.worst_state(order[index:]) == state
    assert ss.worst_state([]) == ss.NO_SAVES


def test_in_sync_end_to_end_decodes_hex_folder_names(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(prefix, "save000")
    result = _check(monkeypatch, tmp_path, FakeService({"save000": T1}))
    assert (result["applicable"], result["state"], result["waiting"]) == (True, ss.IN_SYNC, 0)


def test_only_ever_sends_get_and_never_the_lock(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(prefix, "save000")
    _write(prefix, "save001", dirty=1)
    service = FakeService({"save000": T1, "other": T2})
    _check(monkeypatch, tmp_path, service)
    assert service.calls
    assert {method for method, _url, _headers in service.calls} == {"GET"}
    assert not any("/lock" in url for _method, url, _headers in service.calls)


def test_sends_the_package_family_name(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(prefix, "save000")
    service = FakeService({"save000": T1})
    _check(monkeypatch, tmp_path, service)
    assert service.calls[0][2]["x-xbl-pfn"] == "39EA002F.EXED1_n746a19ndrrjg"


def test_a_manifest_is_read_only_where_the_times_agree(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(prefix, "same")
    _write(prefix, "stale", remote=T2)
    service = FakeService({"same": T1, "stale": T1})
    _check(monkeypatch, tmp_path, service)
    manifests = [url for _m, url, _h in service.calls if url.endswith(",savedgame")]
    assert len(manifests) == 1 and manifests[0].endswith("/same,savedgame")


def test_changed_atom_is_not_called_in_sync(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(prefix, "save000")
    assert _check(monkeypatch, tmp_path, FakeService({"save000": T1}, atoms="OTHER,binary"))["state"] == ss.UNKNOWN


def test_never_uploaded_index_writes_a_dash(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(prefix, "save000", remote="")
    result = _check(monkeypatch, tmp_path, FakeService())
    assert (result["state"], result["waiting"]) == (ss.WAITING_UPLOAD, 1)


def test_another_accounts_saves_do_not_count(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(prefix, "save000", xuid="999", dirty=1)
    service = FakeService()
    result = _check(monkeypatch, tmp_path, service)
    assert (result["state"], result["waiting"]) == (ss.NOT_LAUNCHED, 0)
    assert not service.calls


def test_not_launched_without_a_save_folder(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _check(monkeypatch, tmp_path, FakeService())["state"] == ss.NOT_LAUNCHED


def test_cloud_404_is_empty_not_a_failure(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (prefix / ss.SAVE_FOLDER / XUID / SCID).mkdir(parents=True)
    service = FakeService()
    service.listing = HttpReply(404)
    assert _check(monkeypatch, tmp_path, service)["state"] == ss.NO_SAVES


@pytest.mark.parametrize("status", [400, 403])
def test_refused_listing_with_saves_is_unknown(status: int, prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(prefix, "save000")
    service = FakeService()
    service.listing = HttpReply(status)
    assert _check(monkeypatch, tmp_path, service)["state"] == ss.UNKNOWN


@pytest.mark.parametrize("status", [400, 403])
def test_refused_listing_of_an_empty_folder_is_ignored(status: int, prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (prefix / ss.SAVE_FOLDER / XUID / SCID).mkdir(parents=True)
    service = FakeService()
    service.listing = HttpReply(status)
    assert _check(monkeypatch, tmp_path, service)["state"] == ss.NO_SAVES


def test_more_pages_than_we_read_is_unknown(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(prefix, "save000")
    service = FakeService()
    service.listing = HttpReply(200, {"blobs": [], "pagingInfo": {"continuationToken": "more"}})
    assert _check(monkeypatch, tmp_path, service)["state"] == ss.UNKNOWN


def test_network_failure_is_retried_then_offline(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(prefix, "save000")
    service = FakeService()
    service.listing = HttpReply(None, detail="refused")
    assert _check(monkeypatch, tmp_path, service)["state"] == ss.OFFLINE
    assert len(service.calls) == 3


def test_retry_recovers_from_a_refused_connection(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured: titlestorage sometimes refuses a connection, then answers."""
    _write(prefix, "save000")
    service = FakeService({"save000": T1})
    real = service.__call__
    answers = [HttpReply(None, detail="refused")]

    def flaky(*args: Any, **kwargs: Any) -> HttpReply:
        return answers.pop() if answers else real(*args, **kwargs)

    monkeypatch.setattr(ss, "request_json", flaky)
    assert ss.check_saves(CHAIN, "GAME", _game_dir(tmp_path))["state"] == ss.IN_SYNC


def test_waiting_upload_survives_being_offline(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(prefix, "save000", dirty=1)
    service = FakeService()
    service.listing = HttpReply(None)
    result = _check(monkeypatch, tmp_path, service)
    assert (result["state"], result["waiting"]) == (ss.WAITING_UPLOAD, 1)


def test_no_sign_in_is_offline(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(prefix, "save000")
    assert _check(monkeypatch, tmp_path, FakeService(), chain=None)["state"] == ss.OFFLINE


def test_malformed_index_is_reported_and_left_alone(prefix: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = _write(prefix, "save000")
    (folder / "index").write_text("garbage\n")
    result = _check(monkeypatch, tmp_path, FakeService())
    assert result["state"] == ss.UNKNOWN
    assert (folder / "index").read_text() == "garbage\n"
    assert ss.count_not_uploaded("GAME") == 1


def test_file_save_titles_store_their_name_in_the_index(prefix: Path) -> None:
    folder = prefix / ss.SAVE_FOLDER / XUID / SCID / "not-hex-cache-key"
    folder.mkdir(parents=True)
    (folder / "index").write_text(f"remote {T1}\ndirty 0\nname {b'files'.hex()}\n")
    assert list(ss.read_local(prefix)[XUID][SCID].saves) == ["files"]


def test_count_not_uploaded_covers_every_account(prefix: Path) -> None:
    _write(prefix, "a", dirty=1)
    _write(prefix, "b")
    _write(prefix, "c", remote="")
    _write(prefix, "d", dirty=1, xuid="999")
    assert ss.count_not_uploaded("GAME") == 3


def test_count_not_uploaded_without_a_prefix_is_zero(prefix: Path) -> None:
    assert ss.count_not_uploaded("GAME") == 0


def test_has_local_saves_ignores_empty_folders(prefix: Path) -> None:
    (prefix / ss.SAVE_FOLDER / XUID / SCID).mkdir(parents=True)
    assert ss.has_local_saves("GAME") is False
    _write(prefix, "a", scid="other")
    assert ss.has_local_saves("GAME") is True


@pytest.mark.parametrize(
    ("line", "inode"),
    [
        ("1: FLOCK  ADVISORY  WRITE 1234 08:02:5678 0 EOF\n", 5678),
        ("2: POSIX  ADVISORY  WRITE 1234 08:02:5678 0 EOF\n", None),
        ("3: -> FLOCK  ADVISORY  WRITE 1234 08:02:5678 0 EOF\n", None),
        ("junk\n", None),
    ],
)
def test_lock_line_inode(line: str, inode: int | None) -> None:
    assert ss._lock_line_inode(line) == inode


def test_game_is_running_reads_a_real_flock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    lock = tmp_path / "game.lock"
    lock.touch()
    monkeypatch.setattr(ss, "game_lock_path", lambda _pid: lock)
    assert ss.game_is_running("GAME") is False
    with lock.open() as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        assert ss.game_is_running("GAME") is True
    assert ss.game_is_running("GAME") is False


def test_game_is_running_without_a_lock_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "game_lock_path", lambda _pid: tmp_path / "missing")
    assert ss.game_is_running("GAME") is False


# -- the store's gate and cache ------------------------------------------------


class _Store(save_status_mixin.MicrosoftSaveStatusMixin):
    def __init__(self, enabled: bool = True) -> None:
        self._xbox_pc: Any = types.SimpleNamespace(enabled=lambda: enabled)
        self.builds = 0

        async def build_chain() -> Any:
            self.builds += 1
            return CHAIN

        self._tokens: Any = types.SimpleNamespace(build_chain=build_chain)


def _record(monkeypatch: pytest.MonkeyPatch, record: dict[str, Any] | None) -> None:
    monkeypatch.setattr(save_status_mixin, "record_for_game", lambda _g: record)


INSTALLED = {"productId": "GAME", "state": "installed", "gameDir": "/game"}


@pytest.mark.parametrize(
    ("enabled", "record"),
    [
        (False, INSTALLED),
        (True, None),
        (True, {**INSTALLED, "state": "partial"}),
    ],
)
@pytest.mark.asyncio
async def test_not_applicable_unless_installed_and_enabled(
    enabled: bool, record: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(monkeypatch, record)
    assert (await _Store(enabled).get_xbox_save_status("x"))["applicable"] is False


@pytest.mark.asyncio
async def test_running_skips_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _record(monkeypatch, INSTALLED)
    monkeypatch.setattr(ss, "game_is_running", lambda _p: True)
    store = _Store()
    assert (await store.get_xbox_save_status("x"))["state"] == ss.RUNNING
    assert store.builds == 0


@pytest.mark.asyncio
async def test_results_are_cached_and_force_skips_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    _record(monkeypatch, INSTALLED)
    monkeypatch.setattr(ss, "game_is_running", lambda _p: False)
    checks: list[str] = []

    def fake_check(_chain: Any, pid: str, _dir: Path) -> dict[str, Any]:
        checks.append(pid)
        return {"applicable": True, "state": ss.IN_SYNC, "waiting": 0, "checked_at": 1.0}

    monkeypatch.setattr(ss, "check_saves", fake_check)
    store = _Store()
    await store.get_xbox_save_status("x")
    await store.get_xbox_save_status("x")
    assert len(checks) == 1
    await store.get_xbox_save_status("x", force=True)
    assert len(checks) == 2


@pytest.mark.asyncio
async def test_count_unuploaded_for_an_unknown_game_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _record(monkeypatch, None)
    assert await _Store().count_unuploaded_saves("x") == 0


# -- the RPCs ------------------------------------------------------------------


def _rpc(store: Any, info: Any) -> Any:
    from unifideck.rpc.mixins.xbox_pc import XboxPcRPCMixin

    plugin: Any = XboxPcRPCMixin()
    plugin.registry = types.SimpleNamespace(get=lambda _name: store)
    plugin.sync_service = types.SimpleNamespace(get_game_info=lambda _app: info)
    return plugin


@pytest.mark.asyncio
async def test_uninstall_warning_is_zero_for_other_stores() -> None:
    plugin = _rpc(None, {"store": "gog", "store_game_id": "1"})
    assert await plugin.get_uninstall_save_warning(5) == {"unuploaded": 0}


@pytest.mark.asyncio
async def test_uninstall_warning_is_zero_for_an_unknown_app() -> None:
    assert await _rpc(None, None).get_uninstall_save_warning(5) == {"unuploaded": 0}


@pytest.mark.asyncio
async def test_uninstall_warning_counts_xbox_saves() -> None:
    async def count(game_id: str) -> int:
        assert game_id == "PID"
        return 2

    store = types.SimpleNamespace(count_unuploaded_saves=count)
    plugin = _rpc(store, {"store": "microsoft", "store_game_id": "PID"})
    assert await plugin.get_uninstall_save_warning(5) == {"unuploaded": 2}


@pytest.mark.asyncio
async def test_status_rpc_without_the_store_is_not_applicable() -> None:
    assert (await _rpc(None, None).get_xbox_pc_save_status("x"))["applicable"] is False


# -- the feature line ----------------------------------------------------------


def test_feature_line_true_with_local_containers(prefix: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unifideck.services.cloud_save import support
    from unifideck.stores.microsoft.xodus import ids

    monkeypatch.setattr(ids, "record_for_game", lambda _g: {"productId": "GAME"})
    _write(prefix, "a")
    assert support.resolve_cloud_support("microsoft", "x", {}) is True


def test_feature_line_falls_back_to_the_catalog(prefix: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unifideck.services.cloud_save import support
    from unifideck.stores.microsoft.xodus import ids

    monkeypatch.setattr(ids, "record_for_game", lambda _g: None)
    assert support.resolve_cloud_support("microsoft", "x", {}) is None
    assert support.resolve_cloud_support("microsoft", "x", {"cloud": {"microsoft": False}}) is False


def test_the_store_list_is_unchanged() -> None:
    """Xbox PC has no save strategy: the runtime syncs, and we never push."""
    from unifideck.core.store_capabilities import CLOUD_SAVE_STORES

    assert "microsoft" not in CLOUD_SAVE_STORES
