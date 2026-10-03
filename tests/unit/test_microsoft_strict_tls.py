"""Microsoft endpoints verify TLS: hostname and certificate chain.

Every Microsoft call carries a credential (the OAuth refresh token, an Xbox
Live user token, an XSTS bearer) or returns one. With verification off,
anyone on the same Wi-Fi could impersonate ``login.microsoftonline.com`` and
collect a long-lived ``offline_access`` refresh token. The "outdated Deck cert
store" reason for turning it off is answered by ``ssl_ctx_strict`` loading the
vendored certifi bundle on top of the system store.
"""
from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from unifideck.core.net import ssl_helpers
from unifideck.stores.microsoft.tokens import endpoint

_PKG = Path(__file__).resolve().parents[2] / "py_modules" / "unifideck"
_CREDENTIAL_PACKAGES = (
    _PKG / "stores" / "microsoft",
    _PKG / "services" / "microsoft_ownership",
    _PKG / "services" / "microsoft_subscription",
    _PKG / "auth",
)


def _sources() -> list[Path]:
    return [p for root in _CREDENTIAL_PACKAGES if root.exists() for p in root.rglob("*.py")]


def test_no_credential_module_uses_the_permissive_context() -> None:
    offenders = [
        str(p.relative_to(_PKG))
        for p in _sources()
        if "ssl_ctx_permissive" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_strict_context_verifies_hostname_and_chain() -> None:
    ctx = ssl_helpers.ssl_ctx_strict()
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_strict_context_adds_the_certifi_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[str] = []
    real = ssl.SSLContext.load_verify_locations

    def spy(self: ssl.SSLContext, cafile: str | None = None, *a: object, **k: object) -> None:
        if cafile:
            loaded.append(cafile)
        real(self, cafile, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(ssl_helpers, "_strict_ctx", None)
    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", spy)
    ssl_helpers.ssl_ctx_strict()

    import certifi
    assert loaded == [certifi.where()]


def test_token_endpoint_requests_use_the_strict_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ssl.SSLContext] = []

    def fake_urlopen(request: object, timeout: float, context: ssl.SSLContext) -> object:
        seen.append(context)
        raise OSError("no network in tests")

    monkeypatch.setattr(endpoint.urllib.request, "urlopen", fake_urlopen)
    reply = endpoint.post_form("https://login.microsoftonline.com/x/token", {"a": "b"})

    assert reply.status is None  # a transport failure stays TRANSIENT
    assert seen and seen[0] is ssl_helpers.ssl_ctx_strict()


def test_a_certificate_failure_is_transient_not_a_sign_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(*_a: object, **_k: object) -> object:
        raise ssl.SSLCertVerificationError("certificate verify failed")

    monkeypatch.setattr(endpoint.urllib.request, "urlopen", fake_urlopen)
    reply = endpoint.post_form("https://login.microsoftonline.com/x/token", {})

    state, _ = endpoint.classify_token_reply(reply)
    assert state is endpoint.TokenState.TRANSIENT
