"""RomM pairing, cancellation races, read scopes and owner-only persistence.

The pairing contract is the one the UI depends on: the device code never
leaves the backend, a poll made while another is in flight reports pending
instead of polling twice, and cancelling commits nothing and revokes nothing.
The config cases check the file mode and that a write that fails commits no
connection.
"""
from __future__ import annotations

import asyncio
import json
import stat
import threading

import aiohttp
import pytest

from tests.unit.romm_fakes import Reply, romm_server  # noqa: F401
from unifideck.stores.romm.auth import RomMAuth

URL = "https://romm.test"
PLATFORMS = [
    {"id": 1, "slug": "win", "fs_slug": "win", "display_name": "Windows"},
    {"id": 2, "slug": "win", "fs_slug": "windows9x", "display_name": "Windows 9x"},
    {"id": 3, "slug": "snes", "fs_slug": "snes", "display_name": "SNES"},
]
PAIRING = {
    "device_code": "private-device-code", "user_code": "ABCD1234",
    "verification_path_complete": "/pair/device?user_code=ABCD1234",
    "expires_in": 600, "interval": 5,
}


@pytest.fixture
def config_path(tmp_path):
    return tmp_path / "state" / "romm_config.json"


@pytest.fixture
def auth(config_path):
    return RomMAuth(config_file=str(config_path))


def script(server, *, poll=None):
    server.on("POST", "/api/auth/device/init", Reply(status=201, json=PAIRING))
    server.on("POST", "/api/auth/device/token", poll or Reply(json={"access_token": "paired-secret"}))
    server.on("GET", "/api/platforms", Reply(json=PLATFORMS))
    server.on("GET", "/api/roms", Reply(json={"items": []}))


async def test_pairing_codes_are_backend_owned_until_approval(auth, romm_server, config_path):
    script(romm_server)
    result = await auth.begin("a1", server_url=URL)
    assert result.success and result.action == "pair"
    assert result.metadata["verification_url"] == URL + PAIRING["verification_path_complete"]
    assert result.metadata["user_code"] == "ABCD1234"
    assert result.metadata["interval"] == 5 and result.metadata["expires_in"] == 600
    assert "private-device-code" not in repr(result)
    assert not auth.is_authenticated() and not config_path.exists()
    init = romm_server.calls_to("POST", "/api/auth/device/init")[0]
    assert init.json["requested_scopes"] == ["roms.read", "platforms.read"]
    assert init.json["name"].startswith("Unifideck") and init.headers == {}

    ready = await auth.poll("a1")
    assert ready.success and ready.action == "select_platforms"
    assert {v["fs_slug"] for v in ready.metadata["variants"]} == {"win", "windows9x"}
    assert "paired-secret" not in repr(ready)
    assert not config_path.exists()
    done = await auth.complete("a1", excluded_fs_slugs=["windows9x"])
    assert done.success and auth.is_authenticated()
    saved = json.loads(config_path.read_text())
    assert saved["token"] == "paired-secret" and saved["excluded_fs_slugs"] == ["windows9x"]
    assert "token_id" not in saved and "password" not in saved and "device_code" not in saved
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert (await auth.complete("a1", excluded_fs_slugs=[])).success


@pytest.mark.parametrize("detail", ["authorization_pending", "slow_down"])
async def test_pending_poll_does_not_query_catalog(auth, romm_server, detail):
    script(romm_server, poll=Reply(status=400, json={"detail": detail}))
    await auth.begin("pending", server_url=URL)
    result = await auth.poll("pending")
    assert result.success and result.action == "pending"
    assert romm_server.calls_to("GET", "/api/platforms") == []


@pytest.mark.parametrize("detail,code", [("access_denied", "pairing_denied"), ("expired_token", "pairing_expired")])
async def test_terminal_poll_error_discards_attempt(auth, romm_server, detail, code):
    script(romm_server, poll=Reply(status=400, json={"detail": detail}))
    await auth.begin("terminal", server_url=URL)
    result = await auth.poll("terminal")
    assert not result.success and result.error_code == code
    assert (await auth.poll("terminal")).error_code == "unknown_attempt"


async def test_local_expiry_stops_polling(auth, romm_server, monkeypatch):
    script(romm_server)
    await auth.begin("expiry", server_url=URL)
    monkeypatch.setattr("unifideck.stores.romm.auth.time.monotonic", lambda: float("inf"))
    assert (await auth.poll("expiry")).error_code == "pairing_expired"
    assert not romm_server.calls_to("POST", "/api/auth/device/token")


async def test_transient_poll_failure_can_retry(auth, romm_server):
    script(romm_server, poll=aiohttp.ClientConnectionError("down"))
    await auth.begin("network", server_url=URL)
    result = await auth.poll("network")
    assert result.success and result.action == "pending"
    romm_server._routes = [r for r in romm_server._routes if r[1] != "/api/auth/device/token"]
    romm_server.on("POST", "/api/auth/device/token", Reply(json={"access_token": "retry-token"}))
    assert (await auth.poll("network")).action == "select_platforms"


async def test_cancel_during_approval_never_commits_or_revokes(auth, romm_server, config_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(_call):
        started.set()
        await release.wait()
        return Reply(json={"access_token": "late-secret"})

    script(romm_server, poll=slow)
    await auth.begin("race", server_url=URL)
    task = asyncio.create_task(auth.poll("race"))
    await started.wait()
    assert (await auth.poll("race")).action == "pending"
    await auth.cancel("race")
    release.set()
    assert not (await task).success
    assert not (await auth.complete("race", excluded_fs_slugs=[])).success
    assert not auth.is_authenticated() and not config_path.exists()
    assert not [c for c in romm_server.calls if c.method == "DELETE"]
    assert not romm_server.calls_to("GET", "/api/platforms")


async def test_pairing_code_is_not_forwarded_to_a_redirected_origin(auth, romm_server):
    script(romm_server, poll=Reply(status=307, headers={"Location": "https://other.test/collect"}))
    await auth.begin("redirect", server_url=URL)
    result = await auth.poll("redirect")
    assert result.error_code == "bad_redirect"
    assert not [c for c in romm_server.calls if c.url.startswith("https://other.test")]


async def test_cancel_before_begin_blocks_request(auth, romm_server):
    await auth.cancel("early")
    assert (await auth.begin("early", server_url=URL)).error_code == "cancelled"
    assert not romm_server.calls


async def test_pasted_token_skips_pairing_and_logout_is_local(auth, romm_server, config_path):
    script(romm_server)
    result = await auth.begin("paste", server_url=URL, token=" pasted ", verify_ssl=False)
    assert result.action == "select_platforms"
    assert not romm_server.calls_to("POST", "/api/auth/device/init")
    assert {c.ssl for c in romm_server.calls} == {False}
    assert romm_server.calls_to("GET", "/api/platforms")[0].headers == {"Authorization": "Bearer pasted"}
    assert romm_server.calls_to("GET", "/api/roms")[0].params["platform_ids"] == ["1", "2"]
    await auth.complete("paste", excluded_fs_slugs=[])
    calls = len(romm_server.calls)
    result = await auth.logout()
    assert result.success and result.metadata == {}
    assert len(romm_server.calls) == calls and not config_path.exists()


async def test_identifier_is_reused_after_restart(auth, romm_server, config_path):
    script(romm_server)
    await auth.begin("first", server_url=URL)
    identifier = romm_server.calls[0].json["client_device_identifier"]
    await auth.poll("first")
    await auth.complete("first", excluded_fs_slugs=[])
    reloaded = RomMAuth(str(config_path))
    assert reloaded.is_authenticated()
    await reloaded.begin("second", server_url=URL)
    assert romm_server.calls_to("POST", "/api/auth/device/init")[1].json["client_device_identifier"] == identifier


async def test_cancel_rolls_back_only_matching_committed_attempt(auth, romm_server, config_path):
    script(romm_server)
    await auth.begin("first", server_url=URL, token="t")
    await auth.complete("first", excluded_fs_slugs=[])
    await auth.cancel("unrelated")
    assert auth.is_authenticated()
    await auth.cancel("first")
    assert not auth.is_authenticated() and not config_path.exists()


async def test_persistence_failure_does_not_connect(auth, romm_server, monkeypatch):
    script(romm_server)
    await auth.begin("disk", server_url=URL, token="t")

    def boom(_cfg):
        raise OSError("disk full")

    monkeypatch.setattr(auth, "_write_config", boom)
    assert (await auth.complete("disk", excluded_fs_slugs=[])).error_code == "persist_failed"
    assert not auth.is_authenticated()


async def test_unknown_exclusions_are_rejected(auth, romm_server):
    script(romm_server)
    await auth.begin("exclude", server_url=URL, token="t")
    assert (await auth.complete("exclude", excluded_fs_slugs=["snes"])).error_code == "invalid_exclusion"


@pytest.mark.parametrize("url", ["ftp://romm.test", "https://user:secret@romm.test"])
async def test_invalid_url_rejected_before_requests(auth, romm_server, url):
    assert not (await auth.begin("url", server_url=url)).success
    assert not romm_server.calls


async def test_malformed_token_response_does_not_echo_secrets(auth, romm_server):
    script(romm_server, poll=Reply(json={"wrong_field": "leaky-secret"}))
    await auth.begin("malformed", server_url=URL)
    result = await auth.poll("malformed")
    assert result.error_code == "malformed" and "leaky-secret" not in repr(result)


async def test_cancel_during_commit_reports_failed_removal_and_can_retry(
    auth, romm_server, config_path, monkeypatch,
):
    script(romm_server)
    await auth.begin("commit-race", server_url=URL, token="t")
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_write = auth._write_config
    real_unlink = type(config_path).unlink

    def blocked_write(cfg):
        loop.call_soon_threadsafe(entered.set)
        release.wait(timeout=5)
        real_write(cfg)

    def refuse(self, *args, **kwargs):
        if self == config_path:
            raise PermissionError("read-only")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(auth, "_write_config", blocked_write)
    monkeypatch.setattr(type(config_path), "unlink", refuse)
    complete = asyncio.create_task(auth.complete("commit-race", excluded_fs_slugs=[]))
    await entered.wait()
    cancel = asyncio.create_task(auth.cancel("commit-race"))
    await asyncio.sleep(0)
    release.set()
    assert (await complete).success
    result = await cancel
    assert not result.success and result.error_code == "config_not_removed"
    assert config_path.exists() and auth.is_authenticated()
    monkeypatch.setattr(type(config_path), "unlink", real_unlink)
    assert (await auth.cancel("commit-race")).success
    assert not config_path.exists() and not auth.is_authenticated()


async def test_logout_reports_unremovable_config(auth, romm_server, config_path, monkeypatch):
    script(romm_server)
    await auth.begin("unlink", server_url=URL, token="t")
    await auth.complete("unlink", excluded_fs_slugs=[])
    real_unlink = type(config_path).unlink

    def refuse(self, *args, **kwargs):
        if self == config_path:
            raise PermissionError("read-only")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(type(config_path), "unlink", refuse)
    assert (await auth.logout()).error_code == "config_not_removed"
    assert config_path.exists()
