"""launcher/xbox_pc/argv.py — argv and env for every Xodus command.

The one place that spells the CLI. The flags match the subcommands of our
build (``unifideck-xodus/backend/patches``)::

    xodus-cli account --json
    xodus-cli profile --json
    xodus-cli --external-webview-stdio login
    xodus-cli logout [--device]
    xodus-cli library --json --market M
    xodus-cli resolve-package PID --json --market M
    xodus-cli streaming PID DEST --progress-json --market M --selected-content-id C
    xodus-cli run DIR RUNNER --exe EXE --materialize-chain --market M
    xodus-service

``xodus-service`` takes no options at all: even ``--help`` starts it (I-005).

Every value is validated before an argv is built, so nothing a store or a
file supplies can turn into an extra flag.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from unifideck.core.binaries.cli_env import scrub_cli_env

from .layout import PID_RE, validate_pid

#: A two-letter Store market, upper case.
MARKET_RE = re.compile(r"^[A-Z]{2}\Z")

#: A package content id from ``resolve-package`` (a UUID, any case).
CONTENT_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z",
)

__all__ = [
    "CONTENT_ID_RE",
    "MARKET_RE",
    "PID_RE",
    "account_argv",
    "build_env",
    "library_argv",
    "login_argv",
    "logout_argv",
    "profile_argv",
    "resolve_package_argv",
    "run_argv",
    "service_argv",
    "streaming_argv",
    "validate_content_id",
    "validate_market",
    "validate_pid",
]


def validate_market(market: str) -> str:
    """Return ``market`` when it is two upper-case letters, else ``ValueError``."""
    if not isinstance(market, str) or not MARKET_RE.match(market):
        raise ValueError(f"not a Store market: {market!r}")
    return market


def validate_content_id(content_id: str) -> str:
    """Return ``content_id`` when it is a UUID, else ``ValueError``."""
    if not isinstance(content_id, str) or not CONTENT_ID_RE.match(content_id):
        raise ValueError(f"not a package content id: {content_id!r}")
    return content_id


def _absolute(path: str | Path, what: str) -> str:
    """``path`` as a string, when it is absolute; ``ValueError`` if not."""
    text = str(path)
    if not os.path.isabs(text):
        raise ValueError(f"{what} must be an absolute path: {text!r}")
    return text


def build_env(base: Mapping[str, str], credential_dir: str | Path) -> dict[str, str]:
    """The env for any Xodus process, built on ``base``.

    The loader and Python variables of the frozen plugin host are scrubbed
    (``scrub_cli_env``). ``XODUS_CREDENTIAL_DIR`` must be absolute: a
    relative one makes Xodus fall back to the system keyring without a
    word. Logs stay at error level and plain, and a panic prints no
    backtrace (it could hold token fragments).
    """
    env = scrub_cli_env(dict(base))
    env["XODUS_CREDENTIAL_DIR"] = _absolute(credential_dir, "XODUS_CREDENTIAL_DIR")
    env["XODUS_LOG"] = "error"
    env["NO_COLOR"] = "1"
    env["RUST_BACKTRACE"] = "0"
    return env


def account_argv(cli: str | Path) -> list[str]:
    """Whether a Microsoft account is stored (offline)."""
    return [str(cli), "account", "--json"]


def profile_argv(cli: str | Path) -> list[str]:
    """The stored account's gamertag (one MSA, XSTS and gamerpic fetch)."""
    return [str(cli), "profile", "--json"]


def login_argv(cli: str | Path) -> list[str]:
    """Sign-in, speaking ``NEXUS_AUTH_V1`` frames on stdio."""
    return [str(cli), "--external-webview-stdio", "login"]


def logout_argv(cli: str | Path, *, device: bool = False) -> list[str]:
    """Forget the stored account; ``device`` also drops the device licence."""
    argv = [str(cli), "logout"]
    if device:
        argv.append("--device")
    return argv


def library_argv(cli: str | Path, market: str) -> list[str]:
    """The account's entitlements as one JSON line (can exceed 64 KiB)."""
    return [str(cli), "library", "--json", "--market", validate_market(market)]


def resolve_package_argv(cli: str | Path, pid: str, market: str) -> list[str]:
    """The installable packages of a product (no credentials needed)."""
    return [
        str(cli), "resolve-package", validate_pid(pid),
        "--json", "--market", validate_market(market),
    ]


#: Connections a game download may use at once (``--parallel``).
DOWNLOAD_CONNECTIONS = 8


def streaming_argv(
    cli: str | Path, pid: str, dest: str | Path, market: str, content_id: str,
) -> list[str]:
    """Download and unpack a game into ``dest`` with ``NEXUS_PROGRESS`` lines.

    ``--parallel`` caps the connections. The CDN paces each one to about
    1 to 1.5 MB/s, and our backend splits large files into ranges that share
    this cap (ISSUES.md I-028), so it sets the download speed.
    """
    return [
        str(cli), "streaming", validate_pid(pid), _absolute(dest, "dest"),
        "--progress-json",
        "--parallel", str(DOWNLOAD_CONNECTIONS),
        "--market", validate_market(market),
        "--selected-content-id", validate_content_id(content_id),
    ]


def run_argv(
    cli: str | Path, game_dir: str | Path, runner: str | Path, exe: str, market: str,
    license_cache: str | Path | None = None, offline: bool = False,
) -> list[str]:
    """Run an installed game through the runner script.

    ``exe`` is the game's executable as ``MicrosoftGame.config`` names it.
    ``license_cache`` (backend with offline play only) is where ``run``
    keeps a game's offline licence and reads it when Microsoft is
    unreachable. ``offline`` (Steam is in offline mode) uses a valid
    offline licence without asking Microsoft; without one, ``run`` still
    asks, so offline mode never blocks a launch that works online.
    """
    if not isinstance(exe, str) or not exe or exe.startswith("-") or "\0" in exe:
        raise ValueError(f"not a game executable: {exe!r}")
    argv = [
        str(cli), "run", _absolute(game_dir, "game_dir"), _absolute(runner, "runner"),
        "--exe", exe, "--materialize-chain",
        "--market", validate_market(market),
    ]
    if license_cache is not None:
        argv += ["--license-cache", _absolute(license_cache, "license_cache")]
        if offline:
            argv.append("--offline")
    return argv


def service_argv(service: str | Path) -> list[str]:
    """Start ``xodus-service``. No options exist (I-005)."""
    return [str(service)]
