"""Pure parsers for xodus-cli output (``stores/microsoft/xodus/cli_models``)."""
from __future__ import annotations

import json
from datetime import UTC

import pytest

from unifideck.stores.microsoft.xodus import cli_models as m


def test_account_stored_keeps_email_out_of_repr() -> None:
    status = m.parse_account('warning first\n{"stored": true, "username": "me@example.com"}\n')
    assert status.stored is True
    assert status.username == "me@example.com"
    assert "example.com" not in repr(status)


def test_account_not_stored() -> None:
    assert m.parse_account('{"stored": false}') == m.AccountStatus(stored=False)


@pytest.mark.parametrize("stdout, code", [("", "bad_json"), ("nope", "bad_json"), ('{"x": 1}', "bad_schema")])
def test_account_bad_output(stdout: str, code: str) -> None:
    with pytest.raises(m.ParseError) as err:
        m.parse_account(stdout)
    assert err.value.code == code


def test_profile_drops_the_picture() -> None:
    line = json.dumps({"schema": 1, "account": "a@b.co", "gamertag": " Example Gamer ",
                       "gamerpicDataUrl": "data:image/png;base64," + "A" * 5000})
    profile = m.parse_profile(line)
    assert profile == m.Profile(gamertag="Example Gamer", has_picture=True)


def test_profile_wrong_schema() -> None:
    with pytest.raises(m.ParseError):
        m.parse_profile('{"schema": 2, "gamertag": "x"}')


def test_last_json_object_skips_noise_and_reads_long_lines() -> None:
    big = json.dumps({"rows": ["x" * 100] * 1500})
    assert len(big) > 65536
    assert m.last_json_object(f"log line\n{big}\n") == json.loads(big)
    assert m.last_json_object("{broken\n") is None


def test_redact_removes_tokens_and_emails() -> None:
    text = m.redact("user me.name+x@mail.example.org token " + "a" * 50)
    assert "@" not in text and "aaaa" not in text
    assert "[email]" in text and "[redacted]" in text


def _rec(seq: int, phase: str = "downloading", **extra: object) -> str:
    body = {"schema": 1, "sequence": seq, "phase": phase, "completedBytes": 10,
            "totalBytes": 100, "bytesPerSecond": 5, "paused": False, **extra}
    return "NEXUS_PROGRESS " + json.dumps(body)


@pytest.mark.parametrize("phase", ["initializing", "metadata", "downloading", "finalizing", "complete"])
def test_progress_every_phase(phase: str) -> None:
    rec = m.parse_progress_line(_rec(3, phase))
    assert rec is not None and rec.phase == phase
    assert rec.complete is (phase == "complete")


def test_progress_mark_inside_a_line_and_null_total() -> None:
    line = "prefix noise " + _rec(1, "metadata", totalBytes=None)
    rec = m.parse_progress_line(line)
    assert rec is not None and rec.total_bytes is None and rec.completed_bytes == 10


def test_progress_ignores_text_after_the_record() -> None:
    rec = m.parse_progress_line(_rec(2) + " trailing noise {")
    assert rec is not None and rec.sequence == 2


def test_progress_refuses_stale_wrong_schema_and_junk() -> None:
    assert m.parse_progress_line(_rec(5), after=5) is None
    assert m.parse_progress_line(_rec(6), after=5) is not None
    assert m.parse_progress_line(_rec(1, schema=2)) is None
    assert m.parse_progress_line("NEXUS_PROGRESS {not json") is None
    assert m.parse_progress_line("plain line") is None


def test_rfc3339_nanoseconds_and_z() -> None:
    parsed = m.parse_rfc3339("2026-10-20T18:51:54.508009681+00:00")
    assert parsed is not None and parsed.microsecond == 508009
    z = m.parse_rfc3339("2026-10-20T18:51:54Z")
    assert z is not None and z.tzinfo is not None and z.utcoffset() == UTC.utcoffset(None)


@pytest.mark.parametrize("value", [None, "", "yesterday", "2026-10-20T18:51:54", 5])
def test_rfc3339_refuses(value: object) -> None:
    assert m.parse_rfc3339(value) is None
