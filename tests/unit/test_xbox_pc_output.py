"""Output pumping: never blocks a writer, never logs a secret.

A pipe the launcher stops reading fills at 64 KiB and freezes the game.
``StreamReader.readline`` raises on a line over 64 KiB, which is exactly
how a pump would die. These tests run a real child that writes a 1 MiB
line and check that it exits and the pump survives.
"""
from __future__ import annotations

import asyncio
import io
import sys

import pytest

from unifideck.launcher.xbox_pc.output import (
    MAX_LINE,
    OutputSink,
    classify_failure,
    filter_line,
    game_crashed,
)


@pytest.mark.asyncio
async def test_a_one_mebibyte_line_does_not_block_the_writer() -> None:
    code = (
        "import sys; sys.stdout.write('x' * (1 << 20) + '\\n'); "
        "sys.stdout.write('after\\n'); sys.stdout.flush()"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE,
    )
    log = io.StringIO()
    sink = OutputSink(log)
    pump = asyncio.ensure_future(sink.pump(proc.stdout, source="game"))
    done, _ = await asyncio.wait({pump, asyncio.ensure_future(proc.wait())}, timeout=20)
    assert len(done) == 2, "the writer blocked on a full pipe"
    lines = sink.tail().splitlines()
    assert lines[0].endswith("[cut]")
    assert len(lines[0]) <= MAX_LINE + 10
    assert lines[1] == "after"


@pytest.mark.asyncio
async def test_drains_with_no_log_file() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "print('y' * 300000)", stdout=asyncio.subprocess.PIPE,
    )
    sink = OutputSink(None)
    await asyncio.wait({asyncio.ensure_future(sink.pump(proc.stdout, source="x"))}, timeout=20)
    assert await proc.wait() == 0
    assert sink.tail().endswith("[cut]")


@pytest.mark.asyncio
async def test_lines_split_across_chunks_are_joined() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"hel")
    reader.feed_data(b"lo\nwor")
    reader.feed_data(b"ld")
    reader.feed_eof()
    sink = OutputSink(None)
    await sink.pump(reader, source="t")
    assert sink.tail() == "hello\nworld"


def test_ansi_and_service_disconnect_noise_are_dropped() -> None:
    assert filter_line("\x1b[31mERROR\x1b[0m real problem") == "ERROR real problem"
    assert filter_line("\x1b[2m2026\x1b[0m ERROR Failed to read magic: early eof") is None
    assert filter_line("   \r\n") is None


def test_secrets_never_reach_the_log() -> None:
    log = io.StringIO()
    sink = OutputSink(log)
    token = "eyJ" + "a" * 30 + "." + "b" * 40 + "." + "c" * 40
    sink.write(f"Authorization XBL3.0 x=123;{token}", source="game")
    assert token not in log.getvalue()
    assert token not in sink.tail()


def test_a_closed_log_does_not_stop_the_tail() -> None:
    log = io.StringIO()
    sink = OutputSink(log)
    log.close()
    sink.write("still here")
    assert sink.tail() == "still here"


def test_tail_keeps_the_last_200_lines() -> None:
    sink = OutputSink(None)
    for i in range(250):
        sink.write(f"line {i}")
    lines = sink.tail().splitlines()
    assert len(lines) == 200 and lines[0] == "line 50"


@pytest.mark.parametrize(("tail", "reason"), [
    ("error: not signed in", "sign_in_required"),
    ("NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-parsed 403", "license_denied"),
    ("wine: could not load kernel32.dll", "launch_failed"),
    ("", "launch_failed"),
])
def test_classify_failure(tail: str, reason: str) -> None:
    assert classify_failure(tail) == reason


def test_game_crashed() -> None:
    assert game_crashed(
        "err:vulkan:vkCreateInstance Exception 0xc0000005 in Unix call.\n"
        "wine: Unhandled exception 0xc0000417 in thread f4 at address 7996AF3E (thread 00f4), "
        "starting debugger...",
    )
    assert not game_crashed("game running\nerr:hid:get_container_id_for_usb_udev_device")
