"""The three CLI stores' progress parsers were merged into one — pinned here.

Audit §3.2 recorded "three ETA parsers, three speed parsers, near-verbatim
copies" and ``amazon_progress.py``'s own docstring declined to fold them in
because the shared versions were "close but not byte-identical in their
tokenising (nile's sign handling differs)".

That was true and it was not a reason. Measured against real legendary,
gogdl and nile output the three agreed on **every** line; they diverged only
on two shapes none of the three CLIs emits. This module is that measurement,
kept as a test so the claim stays checked rather than remembered — and so a
future edit to the merged parser is compared against recorded behaviour
rather than against itself.

The two intentional divergences from the pre-merge GOG copy are asserted
explicitly, not skipped: they are the fix, and they should fail loudly if
someone reverts them.
"""

from __future__ import annotations

import pytest

from unifideck.stores.shared.cli_install_helpers import (
    parse_eta_seconds,
    parse_speed_bps,
    parse_transfer_progress,
)

# Real output shapes, one group per CLI. Every one of these was captured
# from the format strings the three tools actually print.
LEGENDARY_LINES = [
    "Progress: 50.5% (1234/2444), Running for 00:01:30, ETA: 00:01:28",
    " + Download\t- 15.50 MiB/s (raw) / 14.00 MiB/s (decompressed)",
    " + Disk\t- 20.00 MiB/s (write) / 0.00 MiB/s (read)",
    "Downloaded: 512.00 MiB, Written: 500.00 MiB",
]
GOGDL_LINES = [
    "[Progress] Progress: 42.50 123456789/987654321, "
    "Running for: 00:01:30, ETA: 00:01:28",
    " + Download\t+ 12.30 MiB/s",
    " + Disk\t+ 30.00 MiB/s (write)",
]
NILE_LINES = [
    "= Progress: 42.50 123456789/987654321, "
    "Running for: 00:01:30, ETA: 00:01:28",
    " + Download\t- 9.75 MiB/s",
]
MALFORMED_LINES = [
    "ETA: ",
    "ETA: --:--",
    "ETA: 1:2:3:4",
    "no eta here",
    "+ Download\tMiB/s",
    "+ Download\tabc MiB/s",
    "Progress:",
    "Progress: 7.0",
    "Progress: 12.5 notanint/999",
]
ALL_LINES = LEGENDARY_LINES + GOGDL_LINES + NILE_LINES + MALFORMED_LINES


# --------------------------------------------------------------------------
# ETA — identical in all three copies before the merge, on every vector.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("line", "expected"), [
    ("Progress: 50.5% (1234/2444), Running for 00:01:30, ETA: 00:01:28", 88),
    ("= Progress: 42.50 1/2, Running for: 00:01:30, ETA: 00:01:28", 88),
    ("ETA: 01:02:03", 3723),
    ("ETA: 02:03", 123),
    ("ETA: ", None),
    ("ETA: --:--", None),
    ("ETA: 1:2:3:4", None),
    ("no eta here", None),
])
def test_eta_matches_the_pre_merge_behaviour(line: str, expected: int | None) -> None:
    """Every value here was produced by all three copies before the merge."""
    assert parse_eta_seconds(line) == expected


# --------------------------------------------------------------------------
# Speed — the merged parser adopts Amazon's tokenising, which was the only
# one of the three that could not report a negative transfer rate.
# --------------------------------------------------------------------------

_MIB = 1024 * 1024


@pytest.mark.parametrize(("line", "expected"), [
    (" + Download\t- 15.50 MiB/s (raw) / 14.00 MiB/s (decompressed)", 15.50 * _MIB),
    (" + Download\t+ 12.30 MiB/s", 12.30 * _MIB),
    (" + Download\t- 9.75 MiB/s", 9.75 * _MIB),
    # "Download" guard: legendary's disk-rate and byte-total lines must not
    # be read as a transfer rate.
    (" + Disk\t- 20.00 MiB/s (write) / 0.00 MiB/s (read)", None),
    ("Downloaded: 512.00 MiB, Written: 500.00 MiB", None),
    ("+ Download\tMiB/s", None),
    ("+ Download\tabc MiB/s", None),
])
def test_speed_matches_the_pre_merge_behaviour(
    line: str, expected: float | None,
) -> None:
    """Real CLI output parses exactly as it did before the merge."""
    assert parse_speed_bps(line) == expected


def test_an_unspaced_minus_no_longer_yields_a_negative_rate() -> None:
    """The one real defect the three-way diff exposed.

    Both the shared and the GOG copy split from the start of the line and
    kept the sign as part of the number when it was not its own token, so
    ``-9.75 MiB/s`` produced a negative bytes/sec that would have rendered
    as a negative MB/s in the download row. Amazon's copy stripped it; the
    merged parser keeps Amazon's behaviour.
    """
    assert parse_speed_bps("+ Download -9.75 MiB/s") == 9.75 * _MIB


def test_a_download_line_without_the_plus_prefix_is_still_read() -> None:
    """GOG's guard required a literal ``+ Download``; the merged one does not.

    The looser guard is Amazon's and the shared copy's. Recorded because it
    is a deliberate widening, not an accident.
    """
    assert parse_speed_bps("Download 5.00 MiB/s") == 5.00 * _MIB


# --------------------------------------------------------------------------
# Transfer progress — gogdl and nile emit the SAME line, which is why the
# two store copies were one function all along.
# --------------------------------------------------------------------------

def test_gogdl_and_nile_progress_lines_parse_identically() -> None:
    """The two differ only by their leading marker (``[Progress]`` vs ``=``)."""
    gog: dict[str, object] = {}
    nile: dict[str, object] = {}
    assert parse_transfer_progress(
        "[Progress] Progress: 42.50 123456789/987654321, "
        "Running for: 00:01:30, ETA: 00:01:28", gog,
    )
    assert parse_transfer_progress(
        "= Progress: 42.50 123456789/987654321, "
        "Running for: 00:01:30, ETA: 00:01:28", nile,
    )
    assert gog == nile == {
        "progress_percent": 42.5,
        "downloaded_bytes": 123456789,
        "total_bytes": 987654321,
        "eta_seconds": 88,
    }


def test_a_speed_line_updates_only_the_rate() -> None:
    """A ``+ Download`` line carries no percentage, and must not zero one."""
    progress: dict[str, object] = {"progress_percent": 42.5}
    assert parse_transfer_progress(" + Download\t+ 12.30 MiB/s", progress)
    assert progress["progress_percent"] == 42.5
    assert progress["speed_bps"] == 12.30 * _MIB


def test_a_progress_line_without_a_byte_pair_still_reports_the_percentage() -> None:
    """``rstrip(',')`` + missing ``/`` was an early-return in both copies."""
    progress: dict[str, object] = {}
    assert parse_transfer_progress("Progress: 7.0 something", progress)
    assert progress["progress_percent"] == 7.0
    assert "downloaded_bytes" not in progress


@pytest.mark.parametrize("line", MALFORMED_LINES)
def test_malformed_lines_never_raise(line: str) -> None:
    """Parsing runs on every line of a live install; it must never throw."""
    progress: dict[str, object] = {}
    parse_transfer_progress(line, progress)
    parse_eta_seconds(line)
    parse_speed_bps(line)


@pytest.mark.parametrize("line", ALL_LINES)
def test_no_line_ever_produces_a_negative_rate(line: str) -> None:
    """The row must never show a negative MB/s, whatever the CLI printed."""
    speed = parse_speed_bps(line)
    assert speed is None or speed >= 0
