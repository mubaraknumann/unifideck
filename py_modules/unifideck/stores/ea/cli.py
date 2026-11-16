"""The only place the plugin runs ``maxima-cli``, the EA client Unifideck bundles.

``maxima-cli`` is our build of Maxima (``unifideck-maxima``). Every command runs
once and ends:

* the data folder (tokens, caches) is ours: ``MAXIMA_DATA_DIR``;
* it never opens a browser or registers desktop handlers
  (``MAXIMA_PACKAGED``, ``MAXIMA_DISABLE_QRC``);
* it starts in an empty private folder. Maxima's cloud sync can read files from
  the working directory (unifideck-maxima ISSUES.md E-003), so nothing may be
  there;
* results come as JSON on stdout; logs, already redacted by the CLI, on stderr.

Token refresh is safe across processes: the CLI locks its own token file
(``unifideck-token-lock``), so the plugin keeps no lock of its own.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unifideck.core.binaries import clean_cli_env
from unifideck.launcher.ea import maxima
from unifideck.stores.shared.cli_install_helpers import terminate_process_tree
from unifideck.stores.shared.cli_json import last_json_object

logger = logging.getLogger(__name__)
_LOG = "[EA/maxima]"

AUTH_TIMEOUT_S = 30.0
LIBRARY_TIMEOUT_S = 90.0
#: A cloud sync fetches the library, then every changed save file.
CLOUD_TIMEOUT_S = 300.0
#: How much of stderr an error keeps (the end, where the reason is).
_TAIL_CHARS = 600


class MaximaCliError(Exception):
    """A ``maxima-cli`` command failed. ``code`` is stable; ``detail`` is for logs."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class _RunOutput:
    returncode: int
    stdout: str
    tail: str


class MaximaCli:
    """Runs ``maxima-cli`` commands for the EA store."""

    def __init__(self, cli_path: str | None, data_dir: Path | None = None) -> None:
        self.cli_path = cli_path
        self.data_dir = data_dir

    async def spawn(
        self, args: list[str], *, merge_stderr: bool = False, env: dict[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        """Start one command in its own session, in the empty run folder.

        ``merge_stderr`` sends the logs into stdout, for streamed commands whose
        reader keeps the tail. ``env`` adds variables, such as a game's prefix.
        The caller owns the process from here.
        """
        if not self.cli_path:
            raise MaximaCliError("cli_missing", "maxima-cli is not installed")
        run_dir = await asyncio.to_thread(maxima.prepare_run_dir, self.data_dir)
        try:
            return await asyncio.create_subprocess_exec(
                self.cli_path, *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
                env=clean_cli_env({**maxima.base_env(self.data_dir), **(env or {})}),
                cwd=str(run_dir),
                start_new_session=True,
            )
        except OSError as e:
            raise MaximaCliError("spawn_failed", f"{args[0]}: {e.strerror or type(e).__name__}") from None

    async def _run(
        self, args: list[str], *, timeout_s: float, env: dict[str, str] | None = None,
    ) -> _RunOutput:
        """Run one command to the end, killing its tree on timeout or cancel."""
        proc = await self.spawn(args, env=env)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            await terminate_process_tree(proc, _LOG)
            raise MaximaCliError("timeout", f"{args[0]} took longer than {timeout_s:.0f}s") from None
        except BaseException:
            await terminate_process_tree(proc, _LOG)
            raise
        tail = stderr.decode(errors="replace")[-_TAIL_CHARS:].strip()
        rc = proc.returncode if proc.returncode is not None else -1
        return _RunOutput(rc, stdout.decode(errors="replace"), tail)

    async def _json(
        self, args: list[str], timeout_s: float, env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        out = await self._run(args, timeout_s=timeout_s, env=env)
        if out.returncode != 0:
            code = "not_signed_in" if "Not signed in" in out.tail else "command_failed"
            raise MaximaCliError(code, f"{args[0]} exited {out.returncode}: {out.tail}")
        value = last_json_object(out.stdout)
        if value is None:
            raise MaximaCliError("bad_output", f"{args[0]} printed no JSON: {out.tail}")
        return value

    # ── Commands ────────────────────────────────────────────────────

    async def auth_url(self) -> str:
        """EA's sign-in URL. The PKCE verifier waits in the data folder."""
        value = await self._json(["auth-url"], AUTH_TIMEOUT_S)
        url = value.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise MaximaCliError("bad_output", "auth-url gave no https URL")
        return url

    async def auth_exchange(self, code: str) -> str | None:
        """Trade the captured code for tokens; returns the EA user id."""
        value = await self._json(["auth-exchange", "--code", code], AUTH_TIMEOUT_S)
        if value.get("signed_in") is not True:
            raise MaximaCliError("exchange_failed")
        user_id = value.get("user_id")
        return str(user_id) if user_id else None

    async def signed_in(self) -> bool:
        """Whether a valid (or refreshable) EA sign-in is stored."""
        value = await self._json(["auth-status"], AUTH_TIMEOUT_S)
        return value.get("signed_in") is True

    async def logout(self) -> None:
        """Delete the stored EA sign-in."""
        await self._json(["logout"], AUTH_TIMEOUT_S)

    async def list_games(self) -> list[dict[str, Any]]:
        """The owned library, one entry per base game (``unifideck-json``)."""
        value = await self._json(["list-games", "--json"], LIBRARY_TIMEOUT_S)
        games = value.get("games")
        if not isinstance(games, list):
            raise MaximaCliError("bad_output", "list-games gave no games list")
        return [g for g in games if isinstance(g, dict)]

    async def live_build_id(self, offer_id: str) -> str | None:
        """The build id EA serves now for ``offer_id`` (``unifideck-json``)."""
        value = await self._json(["available-builds", "--offer-id", offer_id, "--json"], LIBRARY_TIMEOUT_S)
        live = value.get("live")
        build = live.get("buildId") if isinstance(live, dict) else None
        return str(build) if build else None

    async def build_size(self, offer_id: str) -> int | None:
        """Installed bytes of EA's live build, read from its archive index."""
        value = await self._json(["build-size", "--offer-id", offer_id], LIBRARY_TIMEOUT_S)
        size = value.get("install_bytes")
        return int(size) if isinstance(size, int) and size > 0 else None

    async def cloud_paths(self, offer_id: str, prefix: Path) -> dict[str, Any]:
        """The game's save rules and what they select in ``prefix`` (``unifideck-cloudsync``).

        Keys: ``has_cloud_save``, ``rules``, ``excludes``, ``folders``, ``files``.
        Reads only; takes no cloud lock.
        """
        return await self._json(
            ["cloud-paths", "--offer-id", offer_id], LIBRARY_TIMEOUT_S, _prefix_env(prefix),
        )

    async def cloud_sync(
        self, offer_id: str, prefix: Path, *, write: bool, force: bool = False,
    ) -> dict[str, Any]:
        """Pull (or with ``write`` push) the game's cloud saves in ``prefix``.

        Without ``force`` a file changed on both sides transfers nothing and is
        counted in ``conflicts``; the result also has ``downloaded``,
        ``uploaded``, ``unchanged``, ``local_newer`` and ``skipped``.
        """
        args = ["--json", "cloud-sync", "--offer-id", offer_id]
        if write:
            args.append("--write")
        if force:
            args.append("--force")
        return await self._json(args, CLOUD_TIMEOUT_S, _prefix_env(prefix))


    async def achievements(self, offer_id: str) -> list[dict[str, Any]]:
        """EA's achievements for ``offer_id`` and this account's progress (``unifideck-achievements``).

        Each entry: ``id``, ``name``, ``description``, ``how_to``, ``awarded``,
        ``awarded_at``, ``hidden``, ``points``, ``icons``. Empty for a game
        without EA achievements.
        """
        value = await self._json(["achievements", "--offer-id", offer_id], AUTH_TIMEOUT_S)
        items = value.get("achievements")
        if not isinstance(items, list):
            raise MaximaCliError("bad_output", "achievements gave no list")
        return [i for i in items if isinstance(i, dict)]

    async def cloud_status(self, offer_id: str, prefix: Path) -> dict[str, Any]:
        """How ``prefix``'s saves compare with the cloud, file by file (``unifideck-cloud-status``).

        Moves nothing. Keys: ``has_cloud_save``, ``cloud_files``, ``cloud_bytes``,
        ``cloud_modified``, ``unchanged``, ``cloud_newer``, ``local_newer``,
        ``conflicts``, ``cloud_only``, ``local_only``.
        """
        return await self._json(
            ["cloud-status", "--offer-id", offer_id], LIBRARY_TIMEOUT_S, _prefix_env(prefix),
        )

def _prefix_env(prefix: Path) -> dict[str, str]:
    """Save paths resolve inside this prefix; no Proton is run."""
    return {"MAXIMA_WINE_PREFIX": str(prefix)}
