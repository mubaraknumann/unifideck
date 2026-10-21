"""Exact argv and env for every Xodus command (``launcher/xbox_pc/argv``).

The flag spellings come from our build's clap definitions. A typo here is a
CLI usage error on the device and nowhere else, so each builder's output is
pinned in full.
"""
from __future__ import annotations

import inspect

import pytest

from unifideck.core.binaries.cli_env import SCRUBBED_VARS
from unifideck.launcher.xbox_pc import argv

CLI = "/x/backend/0.1.0-b2b95ee8/xodus-cli"
PID = "9PK087LNGJC5"
CID = "1760268b-59d2-488b-8f68-b864a82b23be"


def test_plugin_command_argv() -> None:
    assert argv.account_argv(CLI) == [CLI, "account", "--json"]
    assert argv.profile_argv(CLI) == [CLI, "profile", "--json"]
    assert argv.login_argv(CLI) == [CLI, "--external-webview-stdio", "login"]
    assert argv.logout_argv(CLI) == [CLI, "logout"]
    assert argv.logout_argv(CLI, device=True) == [CLI, "logout", "--device"]
    assert argv.library_argv(CLI, "IN") == [CLI, "library", "--json", "--market", "IN"]
    assert argv.resolve_package_argv(CLI, PID, "US") == [
        CLI, "resolve-package", PID, "--json", "--market", "US",
    ]


def test_streaming_argv() -> None:
    assert argv.streaming_argv(CLI, PID, "/mnt/Xbox/9PK087LNGJC5/game", "IN", CID) == [
        CLI, "streaming", PID, "/mnt/Xbox/9PK087LNGJC5/game", "--progress-json",
        "--market", "IN", "--selected-content-id", CID,
    ]


def test_run_argv() -> None:
    assert argv.run_argv(CLI, "/g/game", "/r/unifideck-xbox-wine", "Balatro.exe", "IN") == [
        CLI, "run", "/g/game", "/r/unifideck-xbox-wine", "--exe", "Balatro.exe",
        "--materialize-chain", "--market", "IN",
    ]


def test_service_argv_takes_no_options() -> None:
    """Even ``--help`` starts the service (I-005), so no option can exist."""
    assert argv.service_argv("/x/xodus-service") == ["/x/xodus-service"]
    assert list(inspect.signature(argv.service_argv).parameters) == ["service"]


@pytest.mark.parametrize("market", ["us", "USA", "", "U1", "US\n", "--x"])
def test_bad_market_is_refused(market: str) -> None:
    with pytest.raises(ValueError):
        argv.library_argv(CLI, market)


@pytest.mark.parametrize("pid", ["--help", "9pk087lngjc5", "9PK087LNGJC5\n", "../x"])
def test_bad_product_id_is_refused(pid: str) -> None:
    with pytest.raises(ValueError):
        argv.resolve_package_argv(CLI, pid, "US")


@pytest.mark.parametrize("cid", ["--x", "not-a-uuid", CID + "\n", CID[:-1]])
def test_bad_content_id_is_refused(cid: str) -> None:
    with pytest.raises(ValueError):
        argv.streaming_argv(CLI, PID, "/d", "US", cid)


def test_upper_case_content_id_is_accepted() -> None:
    assert argv.validate_content_id(CID.upper()) == CID.upper()


def test_relative_paths_are_refused() -> None:
    with pytest.raises(ValueError):
        argv.streaming_argv(CLI, PID, "relative/dest", "US", CID)
    with pytest.raises(ValueError):
        argv.run_argv(CLI, "game", "/r", "a.exe", "US")
    with pytest.raises(ValueError):
        argv.run_argv(CLI, "/g", "runner", "a.exe", "US")


@pytest.mark.parametrize("exe", ["", "-x.exe", "--materialize-chain", "a\0.exe"])
def test_bad_exe_is_refused(exe: str) -> None:
    with pytest.raises(ValueError):
        argv.run_argv(CLI, "/g", "/r", exe, "US")


def test_build_env_scrubs_and_sets_the_xodus_vars() -> None:
    base = dict.fromkeys(SCRUBBED_VARS, "x")
    base.update({"PATH": "/usr/bin", "HOME": "/home/deck", "XODUS_LOG": "trace"})
    env = argv.build_env(base, "/data/xbox_pc/credentials")
    assert not set(SCRUBBED_VARS) & set(env)
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/deck"
    assert env["XODUS_CREDENTIAL_DIR"] == "/data/xbox_pc/credentials"
    assert env["XODUS_LOG"] == "error"
    assert env["NO_COLOR"] == "1"
    assert env["RUST_BACKTRACE"] == "0"
    assert "LD_LIBRARY_PATH" in base, "the caller's mapping is not mutated"


def test_build_env_refuses_a_relative_credential_dir() -> None:
    """A relative dir makes Xodus fall back to the keyring silently."""
    with pytest.raises(ValueError):
        argv.build_env({}, "credentials")
