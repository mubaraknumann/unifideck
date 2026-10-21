"""Xbox PC streaming: progress records, stderr diagnostics, failure codes.

The two pipes stay apart (stderr is unbuffered and written in pieces), a
record is found anywhere in a line, and every failure leaves as one code
that ``download-errors.ts`` can translate.
"""
from __future__ import annotations

import json

import pytest
from _xbox_pc_fakes import CID, PID, XBOX_CID, game_choice, plan, record_line

from unifideck.stores.microsoft.xodus.cli_models import parse_progress_line
from unifideck.stores.microsoft.xodus.diagnostics import Diagnostics, drain_stderr, redact
from unifideck.stores.microsoft.xodus.errors import (
    InstallError,
    credential_failure,
    license_failure,
)
from unifideck.stores.microsoft.xodus.package_choice import select_content
from unifideck.stores.microsoft.xodus.progress import ProgressMapper, queue_payload
from unifideck.stores.shared.cli_install_helpers import TailRingBuffer


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _mapper() -> tuple[ProgressMapper, _Clock, TailRingBuffer]:
    clock, tail = _Clock(), TailRingBuffer()
    return ProgressMapper(tail, clock=clock), clock, tail


def _line(*args: object, **kw: object) -> str:
    return record_line(*args, **kw).decode().strip()  # type: ignore[arg-type]


# ── progress mapping ────────────────────────────────────────────────────
def test_phases_map_to_the_queue_phases() -> None:
    mapper, clock, _ = _mapper()
    seen = []
    for seq, phase in enumerate(("initializing", "metadata", "downloading", "finalizing")):
        clock.now += 0.1
        seen.append(mapper.feed(_line(seq, phase, 0, 10 if phase != "initializing" else None)))
    assert [u["phase"] for u in seen if u] == ["preparing", "preparing", "downloading", "verifying"]


def test_no_percentage_before_the_total_is_known() -> None:
    mapper, _, _ = _mapper()
    update = mapper.feed(_line(0, "metadata"))
    assert update == {"phase": "preparing", "speed_bps": 0.0}


def test_bytes_speed_and_eta_once_downloading() -> None:
    mapper, _, _ = _mapper()
    update = mapper.feed(_line(3, "downloading", 250, 1000, 50))
    assert update == {"phase": "downloading", "speed_bps": 50.0, "downloaded_bytes": 250,
                      "total_bytes": 1000, "percentage": 25.0, "eta_seconds": 15}


def test_an_update_with_nothing_to_fetch_reads_100_percent() -> None:
    record = parse_progress_line(_line(1, "downloading", 0, 0))
    assert record is not None
    assert queue_payload(record)["percentage"] == 100.0


def test_throttled_to_once_a_second_within_a_phase() -> None:
    mapper, clock, _ = _mapper()
    assert mapper.feed(_line(1, "downloading", 1, 100)) is not None
    clock.now += 0.2
    assert mapper.feed(_line(2, "downloading", 2, 100)) is None
    clock.now += 1.0
    assert mapper.feed(_line(3, "downloading", 3, 100)) is not None


def test_a_phase_change_and_the_end_are_always_sent() -> None:
    mapper, _, _ = _mapper()
    assert mapper.feed(_line(1, "downloading", 1, 100)) is not None
    assert mapper.feed(_line(2, "finalizing", 100, 100)) is not None
    assert mapper.feed(_line(3, "complete", 100, 100)) is not None
    assert mapper.terminal == "complete"


def test_out_of_order_and_malformed_records_are_dropped() -> None:
    mapper, clock, _ = _mapper()
    assert mapper.feed(_line(5, "downloading", 1, 100)) is not None
    clock.now += 5
    assert mapper.feed(_line(5, "downloading", 2, 100)) is None
    assert mapper.feed(_line(4, "finalizing", 2, 100)) is None
    assert mapper.feed("NEXUS_PROGRESS {not json") is None
    bad_schema = json.dumps({"schema": 2, "sequence": 9, "phase": "downloading"})
    assert mapper.feed(f"NEXUS_PROGRESS {bad_schema}") is None
    assert mapper.feed(_line(9, "teleporting", 2, 100)) is None
    assert mapper.phase == "downloading"


def test_a_record_after_a_stray_fragment_is_still_read() -> None:
    mapper, _, _ = _mapper()
    update = mapper.feed("NEXUS_CONTENT_LICENSE_DIAG" + _line(2, "downloading", 5, 10))
    assert update is not None and update["downloaded_bytes"] == 5


def test_quiet_phases_widen_the_stall_window() -> None:
    mapper, _, _ = _mapper()
    assert mapper.in_quiet_phase() is False  # before the first record
    mapper.feed(_line(0, "metadata"))
    assert mapper.in_quiet_phase() is True
    mapper.feed(_line(1, "downloading", 0, 10))
    assert mapper.in_quiet_phase() is False
    mapper.feed(_line(2, "finalizing", 10, 10))
    assert mapper.in_quiet_phase() is True


async def test_non_record_lines_go_to_the_tail_redacted() -> None:
    mapper, _, tail = _mapper()
    sent: list[dict[str, object]] = []

    async def cb(update: dict[str, object]) -> None:
        sent.append(update)

    await mapper.handle_line("GET https://cdn.example/pkg?sig=SECRET failed", PID, cb)
    await mapper.handle_line(_line(1, "downloading", 1, 2), PID, cb)
    assert "SECRET" not in tail.tail() and "?[query]" in tail.tail()
    assert len(sent) == 1


# ── stderr diagnostics ──────────────────────────────────────────────────
class _Chunks:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


async def test_stderr_lines_split_across_writes_are_rebuilt() -> None:
    diags, tail = Diagnostics(), TailRingBuffer()
    await drain_stderr(_Chunks([
        b"NEXUS_CONTENT_LICENSE_DIAG", b"NOSTIC_V1 content-http 4", b"03\nplain error\n",
    ]), tail, diags)
    assert [(d.stage, d.status) for d in diags.licenses()] == [("content-http", 403)]
    assert "plain error" in tail.tail()


async def test_an_overlong_stderr_line_is_cut_not_fatal() -> None:
    diags, tail = Diagnostics(), TailRingBuffer()
    await drain_stderr(_Chunks([b"x" * 200_000, b"\nNEXUS_CREDENTIAL_ERROR_V1 credential-io\n"]),
                       tail, diags)
    assert diags.credential_errors() == ["credential-io"]


def test_redact_hides_tokens_and_query_strings() -> None:
    text = redact("token eyJ" + "a" * 60 + " at https://x.test/p?code=abc")
    assert "aaaa" not in text and "code=abc" not in text


# ── failure codes ───────────────────────────────────────────────────────
def _diags(*lines: str) -> Diagnostics:
    diags = Diagnostics()
    for line in lines:
        diags.add(line)
    return diags


@pytest.mark.parametrize(("line", "code"), [
    ("NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-http 401", "xbox_signin_required"),
    ("NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-http 403", "xbox_license_denied"),
    ("NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-http 429", "xbox_license_rate_limited"),
    ("NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-http 503", "xbox_license_unavailable"),
    ("NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-http 418", "xbox_license_invalid"),
    ("NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-timeout 0", "xbox_license_unavailable"),
    ("NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-not-entitled 200", "xbox_license_denied"),
    ("NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-xml 200", "xbox_license_invalid"),
])
def test_license_stages(line: str, code: str) -> None:
    assert license_failure(_diags(line)) == code


def test_a_later_parsed_license_clears_an_earlier_failure() -> None:
    diags = _diags("NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-http 503",
                   "NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-parsed 200")
    assert license_failure(diags) is None


def test_credential_errors() -> None:
    assert credential_failure(_diags("NEXUS_CREDENTIAL_ERROR_V1 keyring-missing-data")) == (
        "xbox_signin_required")
    assert credential_failure(_diags("NEXUS_CREDENTIAL_ERROR_V1 credential-io")) == (
        "xbox_credentials_unavailable")
    assert credential_failure(_diags("plain line")) is None


# ── package choice ──────────────────────────────────────────────────────
def test_the_one_supported_pc_game_is_picked() -> None:
    assert select_content(plan(), PID) == CID


def test_zero_supported_games_is_unavailable() -> None:
    only_xbox = plan(game_choice(XBOX_CID, platform="Windows.Xbox", supported=False))
    with pytest.raises(InstallError) as err:
        select_content(only_xbox, PID)
    assert err.value.code == "xbox_package_unavailable"


def test_two_editions_are_refused_not_guessed() -> None:
    other = "11111111-2222-3333-4444-555555555555"
    with pytest.raises(InstallError) as err:
        select_content(plan(game_choice(CID), game_choice(other), status="choice"), PID)
    assert err.value.code == "xbox_package_choice"


def test_the_recorded_content_id_wins_among_editions() -> None:
    other = "11111111-2222-3333-4444-555555555555"
    assert select_content(plan(game_choice(CID), game_choice(other)), PID, other) == other


def test_add_ons_and_bad_ids_are_not_games() -> None:
    choices = plan(game_choice(CID, kind="GameAddOn"), game_choice("not-a-uuid"))
    with pytest.raises(InstallError):
        select_content(choices, PID)


@pytest.mark.parametrize("bad", [
    {"schema": 2}, plan(root="9ZZZZZZZZZZZ"), plan(status="weird"), [],
])
def test_a_bad_plan_envelope_is_a_resolve_failure(bad: object) -> None:
    with pytest.raises(InstallError) as err:
        select_content(bad, PID)
    assert err.value.code == "xbox_resolve_failed"
