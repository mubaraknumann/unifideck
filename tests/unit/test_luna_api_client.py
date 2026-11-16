"""``luna_api.client``: the token + ``getPage`` requests and their error mapping.

The HTTP session is injected (``session_factory``), so every request the
client sends is recorded and every response is scripted. Cookie reading is
replaced by a stub so no profile is touched.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from unifideck.stores.luna.luna_api import (
    LunaApiClient,
    LunaApiError,
    LunaApiShapeError,
    LunaSessionError,
    resolve_region,
)
from unifideck.stores.luna.luna_api import client as client_mod

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "luna" / "getpage_my_games.json"
_TOKEN = "dummy-access-token"
_TEMPO = "https://proxy-prod.eu-west-1.tempo.digital.a2z.com/getPage"


class _Response:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Session:
    """Records requests; answers from ``routes`` (url → response or exception)."""

    def __init__(self, routes: dict[str, Any], log: list[dict[str, Any]]) -> None:
        self._routes = routes
        self._log = log

    def request(self, method: str, url: str, *, headers: dict[str, str], data: str | None) -> Any:
        self._log.append({"method": method, "url": url, "headers": headers, "data": data})
        answer = self._routes[url]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _ok_token() -> _Response:
    return _Response(200, json.dumps({"invalidReason": None, "token": _TOKEN}))


def _client(
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, Any],
    *,
    cookies: dict[str, str] | None = None,
    locale: str = "",
) -> tuple[LunaApiClient, list[dict[str, Any]]]:
    log: list[dict[str, Any]] = []
    jar = {"at-main": "cookie-a", "ubid-main": "cookie-b"} if cookies is None else cookies
    monkeypatch.setattr(client_mod, "read_cookies", lambda _p, _s: dict(jar))
    region = resolve_region("https://luna.amazon.de")
    assert region is not None
    client = LunaApiClient(
        region, "/profile", locale=locale,
        session_factory=lambda: _Session(routes, log),  # type: ignore[arg-type,return-value]
    )
    return client, log


async def test_chosen_locale_overrides_the_site_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, log = _client(
        monkeypatch, _routes(_Response(200, _FIXTURE.read_text())), locale="en_GB",
    )
    await client.fetch_library()
    assert log[1]["headers"]["x-amz-locale"] == "en_GB"


def _routes(get_page: Any, token: Any = None) -> dict[str, Any]:
    return {
        "https://luna.amazon.de/token": token or _ok_token(),
        _TEMPO: get_page,
    }


# ── happy path ───────────────────────────────────────────────────────


async def test_fetch_library_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _FIXTURE.read_text(encoding="utf-8")
    client, log = _client(monkeypatch, _routes(_Response(200, body)))

    library = await client.fetch_library()

    assert len(library.titles) == 6
    assert library.titles[0].browser_url.startswith("https://luna.amazon.de/game/")
    token_req, page_req = log
    assert token_req["method"] == "GET"
    assert token_req["url"] == "https://luna.amazon.de/token"
    assert token_req["headers"]["Cookie"] == "at-main=cookie-a; ubid-main=cookie-b"
    assert "Windows NT 10.0" in token_req["headers"]["User-Agent"]
    assert page_req["method"] == "POST"
    assert page_req["url"] == _TEMPO


async def test_get_page_request_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    client, log = _client(monkeypatch, _routes(_Response(200, _FIXTURE.read_text())))

    await client.fetch_library()

    page_req = log[1]
    headers = page_req["headers"]
    assert headers["Content-Type"] == "text/plain;charset=UTF-8"
    assert headers["x-amz-access-token"] == _TOKEN
    # No locale passed: the country site's own default (luna.amazon.de → de_DE).
    assert headers["x-amz-locale"] == "de_DE"
    assert headers["x-amz-marketplace-id"] == "A1PA6795UKMFR9"
    assert headers["x-amz-country-of-residence"] == "DE"
    assert headers["x-amz-device-type"] == "browser"
    assert headers["x-amz-platform"] == "web"
    assert headers["Origin"] == "https://luna.amazon.de"
    assert headers["Referer"] == "https://luna.amazon.de/"
    lowered = {k.lower() for k in headers}
    for session_bound in ("x-amz-tdi", "x-amz-session-id", "x-amz-device-serial-number",
                          "x-amz-profile-token", "cookie"):
        assert session_bound not in lowered
    body = json.loads(page_req["data"])
    assert body == client_mod.GET_PAGE_BODY
    assert body["searchContext"] == {"query": "my_games", "sort": "NEW_RELEASES", "filtersList": []}
    assert body["pageContext"] == {"pageType": "multistate_browse_results", "pageId": "default"}
    meta = body["clientContext"]["browserMetadata"]
    assert meta["osName"] == "Windows"
    assert meta["browserVersion"] == client_mod.BROWSER_VERSION
    assert f"Chrome/{client_mod.BROWSER_VERSION}" in headers["User-Agent"]


def test_browser_version_tracks_the_downloaded_chromium() -> None:
    from unifideck.auth.edge_browser.chrome_installer import CHROME_VERSION

    assert client_mod.BROWSER_VERSION == CHROME_VERSION.split(".")[0] + ".0.0.0"


# ── token errors ─────────────────────────────────────────────────────


async def test_no_cookies_is_a_session_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client, log = _client(monkeypatch, _routes(_Response(200, "{}")), cookies={})
    with pytest.raises(LunaSessionError) as exc:
        await client.fetch_library()
    assert exc.value.reason == "no_cookies"
    assert log == []


async def test_unauthenticated_token_is_a_session_error(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _Response(200, json.dumps({"invalidReason": "Unauthenticated", "token": ""}))
    client, log = _client(monkeypatch, _routes(_Response(200, "{}"), token=token))
    with pytest.raises(LunaSessionError) as exc:
        await client.fetch_library()
    assert exc.value.reason == "Unauthenticated"
    assert len(log) == 1  # getPage never sent


async def test_empty_token_is_a_session_error(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _Response(200, json.dumps({"invalidReason": None, "token": ""}))
    client, _log = _client(monkeypatch, _routes(_Response(200, "{}"), token=token))
    with pytest.raises(LunaSessionError):
        await client.fetch_access_token()


@pytest.mark.parametrize("token", [
    _Response(500, "oops"),
    _Response(200, "not json"),
    _Response(200, json.dumps(["not", "an", "object"])),
])
async def test_broken_token_response_is_an_api_error(
    monkeypatch: pytest.MonkeyPatch, token: _Response,
) -> None:
    client, _log = _client(monkeypatch, _routes(_Response(200, "{}"), token=token))
    with pytest.raises(LunaApiError) as exc:
        await client.fetch_access_token()
    assert not isinstance(exc.value, LunaSessionError)


# ── getPage errors ───────────────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403])
async def test_get_page_unauthorised_is_a_session_error(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    client, _log = _client(monkeypatch, _routes(_Response(status, "")))
    with pytest.raises(LunaSessionError) as exc:
        await client.fetch_library()
    assert exc.value.reason == f"http_{status}"


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
async def test_get_page_http_error_is_an_api_error(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    body = '{"message":"Required header [x-amz-locale] is missing"}'
    client, _log = _client(monkeypatch, _routes(_Response(status, body)))
    with pytest.raises(LunaApiError) as exc:
        await client.fetch_library()
    assert not isinstance(exc.value, LunaSessionError)
    assert str(status) in str(exc.value)


@pytest.mark.parametrize("error", [TimeoutError(), aiohttp.ClientConnectionError("refused")])
async def test_transport_error_is_an_api_error(
    monkeypatch: pytest.MonkeyPatch, error: BaseException,
) -> None:
    client, _log = _client(monkeypatch, _routes(error))
    with pytest.raises(LunaApiError):
        await client.fetch_library()


async def test_bad_json_is_an_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _log = _client(monkeypatch, _routes(_Response(200, "<html>")))
    with pytest.raises(LunaApiError):
        await client.fetch_library()


async def test_shape_drift_is_a_shape_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _log = _client(monkeypatch, _routes(_Response(200, '{"pageMemberGroups": {}}')))
    with pytest.raises(LunaApiShapeError):
        await client.fetch_library()


async def test_token_is_never_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client, _log = _client(monkeypatch, _routes(_Response(200, _FIXTURE.read_text())))
    with caplog.at_level("DEBUG"):
        await client.fetch_library()
    assert _TOKEN not in caplog.text
    assert "cookie-a" not in caplog.text


async def test_default_session_uses_strict_tls() -> None:
    session = client_mod._default_session()
    try:
        assert session.timeout.total == 20.0
        connector = session.connector
        assert connector is not None
        from unifideck.core.net import ssl_ctx_strict

        assert connector._ssl is ssl_ctx_strict()  # noqa: SLF001
    finally:
        await session.close()
