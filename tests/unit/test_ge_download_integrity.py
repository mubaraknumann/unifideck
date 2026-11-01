"""Regression: a truncated GE-Proton download must not be treated as success.

``_download`` streams the response until ``read()`` returns empty. A
connection that drops mid-body ends that loop exactly like a clean EOF, so
the short file was written out and returned as a SUCCESS — no exception, no
retry. ``Content-Length`` was already being read into ``total`` for the
progress callback, and simply never compared against what actually arrived.

The consequence is not a tidy "download failed" message. The truncated
tarball extracts far enough to produce a tool directory, which is then
promoted into ``compatibilitytools.d`` and handed to umu, whose
``CompatLayer.__init__`` does ``vdf.load(f)["manifest"]`` on a
half-written ``toolmanifest.vdf`` and dies with an unhandled
``KeyError: 'manifest'`` (umu-launcher#706).

Raising ``OSError`` is deliberate: ``_download_with_retry`` already catches
it, so a short read costs one retry with backoff instead of a broken
install that persists until the user notices.
"""
from __future__ import annotations

import pytest

from unifideck.launcher.proton.infrastructure import ge_download


class _FakeResponse:
    """Minimal ``urlopen`` stand-in serving ``body`` under a declared length."""

    def __init__(self, body: bytes, declared: int | None) -> None:
        self._body = body
        self._pos = 0
        self.headers = (
            {} if declared is None else {"Content-Length": str(declared)}
        )

    def read(self, size: int = -1) -> bytes:
        if self._pos >= len(self._body):
            return b""
        end = len(self._body) if size < 0 else self._pos + size
        chunk = self._body[self._pos:end]
        self._pos = end
        return chunk

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def _serve(monkeypatch):
    """Patch urlopen to return a response of our choosing."""
    def _install(body: bytes, declared: int | None):
        def _fake_urlopen(_req, *_a, **_kw):
            return _FakeResponse(body, declared)
        monkeypatch.setattr(
            ge_download.urllib.request, "urlopen", _fake_urlopen,
        )
    return _install


def test_short_body_against_declared_length_raises(_serve, tmp_path):
    """The actual bug: 500 of 1000 bytes arrive and nothing complains."""
    _serve(b"x" * 500, declared=1000)
    dest = tmp_path / "GE-Proton.tar.gz"

    with pytest.raises(OSError, match="truncated"):
        ge_download.download("https://example/GE.tar.gz", dest, None)


def test_complete_body_is_accepted(_serve, tmp_path):
    _serve(b"x" * 1000, declared=1000)
    dest = tmp_path / "GE-Proton.tar.gz"

    ge_download.download("https://example/GE.tar.gz", dest, None)

    assert dest.stat().st_size == 1000


def test_absent_content_length_is_not_treated_as_truncation(_serve, tmp_path):
    """Chunked/unknown-length responses are legitimate — don't reject them."""
    _serve(b"x" * 700, declared=None)
    dest = tmp_path / "GE-Proton.tar.gz"

    ge_download.download("https://example/GE.tar.gz", dest, None)

    assert dest.stat().st_size == 700


def test_retry_wrapper_converts_truncation_into_a_retry(_serve, tmp_path, monkeypatch):
    """A short read must cost a retry, not a silently broken install."""
    monkeypatch.setattr(ge_download.time, "sleep", lambda _s: None)
    _serve(b"x" * 10, declared=999)
    dest = tmp_path / "GE-Proton.tar.gz"

    ok = ge_download.download_with_retry(
        "https://example/GE.tar.gz", dest, None, attempts=2,
    )

    assert ok is False
    assert not dest.exists(), "a failed download must not leave a partial file"
