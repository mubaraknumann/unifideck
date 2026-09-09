"""GOG install pipeline orchestrator.

``GOGInstaller`` orchestrates a full install through four phases:

1. **preflight** — verify gogdl binary, resolve base path, build the
   ``_InstallContext``;
2. **probe & prepare** — refresh tokens, wipe stale manifests, query
   game info, determine install mode (fresh / update);
3. **run gogdl** — execute the subprocess with progress monitoring,
   followed by a repair pass;
4. **finalize** — locate the install dir, write the marker,
   verify completeness, regenerate the manifest.

``_InstallContext`` is the pivot dataclass that carries state between
phases without inflating the method signatures. Errors at any phase
are wrapped into ``InstallResult`` envelopes with phase-specific error
codes so the UI can pinpoint failures.

Sub-modules used : ``planner`` (mode determination), ``progress``
(subprocess monitoring), ``marker`` (post-install bookkeeping),
``helpers`` (game info probe + language picking),
``uninstall_pipeline`` (symmetric removal).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from unifideck.core.types import InstallResult, Result
from unifideck.stores.gog.config import GOGConfig
from unifideck.stores.gog.tokens import GOGTokenManager

from .helpers import _InstallHelpers
from .marker import _PostInstallMarker
from .planner import INSTALL_MODE_BLOCKED, GOGInstallPlanner
from .progress import _GogdlProgressMonitor, format_gogdl_error
from .uninstall_pipeline import _UninstallPipeline

logger = logging.getLogger(__name__)

@dataclass
class _InstallContext:
    """Install context."""

    game_id: str
    base_path: str
    preferred_lang: str
    explicit_lang: bool
    progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None
    platform: str = ""
    folder_name: str | None = None
    supported_langs: list[str] = field(default_factory=list)
    existing_dirs: set[Any] = field(default_factory=set)
    support_dir: str = ""
    install_mode: str = ""
    found_path: str = ""

class GOGInstaller:
    """Goginstaller."""

    def __init__(
        self,
        config: GOGConfig,
        tokens: GOGTokenManager,
        gogdl_bin: str,
        exe_finder: Callable[[str], str | None],
        locale_fn: Callable[[], str],
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._tokens = tokens
        self._gogdl_bin = gogdl_bin
        self._find_exe = exe_finder
        self._locale_fn = locale_fn
        self._planner = GOGInstallPlanner(config, tokens)
        self._planner.set_gogdl_bin(gogdl_bin)
        self._uninstall_pipeline = _UninstallPipeline(self)
        self._progress_monitor = _GogdlProgressMonitor(self)
        self._marker = _PostInstallMarker(self)
        self._helpers = _InstallHelpers(self)

    async def uninstall_game(
        self,
        game_id: str,
        install_path: str | None = None,
    ) -> Result:
        """Uninstall game."""
        return await self._uninstall_pipeline.uninstall_game(
            game_id,
            install_path,
        )

    async def _run_gogdl_repair_pass(
        self,
        game_id: str,
        platform: str,
        base_path: str,
        folder_name: str | None,
        preferred_lang: str,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        """Run GOGDL repair pass."""
        await self._progress_monitor.run_gogdl_repair_pass(
            game_id,
            platform,
            base_path,
            folder_name,
            preferred_lang,
            progress_cb,
        )

    def _snapshot_dirs(self, base_path: str) -> set[Any]:
        """Snapshot dirs."""
        return self._marker.snapshot_dirs(base_path)

    async def _locate_install(
        self,
        game_id: str,
        base_path: str,
        folder_name: str | None,
        existing_dirs: set[Any],
    ) -> str | None:
        """Locate install."""
        return await self._marker.locate_install(
            game_id,
            base_path,
            folder_name,
            existing_dirs,
        )

    async def _write_install_marker(
        self,
        install_path: str,
        game_id: str,
        language: str,
    ) -> bool:
        """Write install marker."""
        return await self._marker.write_install_marker(
            install_path,
            game_id,
            language,
        )

    async def _regenerate_manifest(self, game_id: str, platform: str) -> None:
        """Regenerate manifest."""
        await self._marker.regenerate_manifest(game_id, platform)

    def _install_failed(
        self,
        game_id: str,
        error: str,
        *,
        cleanup_path: str | None = None,
        cleanup_folder: str | None = None,
    ) -> InstallResult:
        """Install failed."""
        if cleanup_path is not None:
            self._cleanup_partial(cleanup_path, cleanup_folder)
        return InstallResult(
            success=False,
            error=error,
            store="gog",
            game_id=game_id,
        )

    async def install_game(
        self,
        game_id: str,
        base_path: str | None = None,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        language: str | None = None,
    ) -> InstallResult:
        """Install game."""
        logger.info("[GogInstall] install_game game_id=%s base_path=%s language=%s",
                     game_id, base_path, language)
        ctx, failure = self._install_preflight(
            game_id,
            base_path,
            progress_cb,
            language,
        )
        if failure is not None:
            return cast("InstallResult", failure)
        auth_failure = await self._install_probe_and_prepare(ctx)
        if auth_failure is not None:
            return auth_failure
        download_failure = await self._install_run_gogdl_phase(ctx)
        if download_failure is not None:
            return download_failure
        return await self._install_finalize(ctx)

    def _install_preflight(
        self,
        game_id: str,
        base_path: str | None,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
        language: str | None,
    ) -> tuple[Any, ...]:
        """Install preflight."""
        if not Path(self._gogdl_bin).is_file():
            return None, self._install_failed(
                game_id,
                "gogdl_not_found",
            )
        resolved_base = base_path or str(Path(self._config.download_dir).expanduser())
        Path(resolved_base).mkdir(parents=True, exist_ok=True)
        preferred_lang = language or self._locale_fn() or "en-US"
        explicit_lang = language is not None
        logger.info(
            "[GOGInstaller] start: game=%s path=%s lang=%s (explicit=%s)",
            game_id,
            resolved_base,
            preferred_lang,
            explicit_lang,
        )
        ctx = _InstallContext(
            game_id=game_id,
            base_path=resolved_base,
            preferred_lang=preferred_lang,
            explicit_lang=explicit_lang,
            progress_cb=progress_cb,
        )
        return ctx, None

    async def _install_probe_and_prepare(
        self,
        ctx: _InstallContext,
    ) -> InstallResult | None:
        """Install probe and prepare.

        Refactor history (2026-05-14): extracted
        ``_setup_support_dir`` to bring the fan-out under the
        10-callee cap (was 12). The filesystem-prep concerns
        (Path/expanduser/to_thread/makedirs) collapse cleanly
        into one helper since they're a single semantic step.
        """
        if not self._tokens.has_tokens:
            await self._tokens.load()
        if not await self._tokens.refresh_if_stale():
            return self._install_failed(
                ctx.game_id,
                "not_authenticated",
            )
        await self._wipe_support_cache(ctx.game_id)
        (
            ctx.platform,
            ctx.folder_name,
            ctx.supported_langs,
        ) = await self._helpers.probe_game_info(ctx.game_id)
        ctx.existing_dirs = self._snapshot_dirs(ctx.base_path)
        await self._setup_support_dir(ctx)
        target_folder = (
            str(Path(ctx.base_path) / ctx.folder_name) if ctx.folder_name else None
        )
        ctx.install_mode = await self._planner.determine_install_mode(
            ctx.game_id,
            target_folder,
        )
        # The target directory holds another store's install. Every other mode
        # would either delete it or download over it, so stop here: the user
        # picks a different location (or uninstalls the other game) instead of
        # discovering afterwards that their files are gone.
        if ctx.install_mode == INSTALL_MODE_BLOCKED:
            return self._install_failed(
                ctx.game_id,
                "install_folder_owned_by_another_store",
            )
        # Wipe the local manifest ONLY for a fresh download. A stale manifest
        # left over from a prior uninstall would make gogdl ``download`` report
        # "Nothing to do" and skip the transfer, so a fresh install must clear
        # it. But gogdl ``repair`` REQUIRES that manifest — wiping it makes it
        # crash with "No manifest stored locally" and the existing valid install
        # then gets deleted. So keep the manifest whenever we're repairing.
        if ctx.install_mode == "download":
            await self._wipe_manifests(ctx.game_id)
        return None

    async def _setup_support_dir(self, ctx: _InstallContext) -> None:
        """Compute ``ctx.support_dir`` and ``mkdir -p`` it.

        Pulled out of ``_install_probe_and_prepare`` to keep
        that function under the 10-callee fan-out cap. Groups
        the four filesystem primitives (Path / expanduser /
        to_thread / makedirs) into one semantic step:
        "ensure the gog-support directory for this game
        exists on disk".

        The ``asyncio.to_thread`` indirection is there because
        ``Path.expanduser`` can do I/O on some platforms
        (pwd database lookups on POSIX) — pushing it off the
        event loop is cheap and avoids the rare blocking call.
        """
        gogdl_config_dir = await asyncio.to_thread(
            lambda: str(Path(self._config.gogdl_config_dir).expanduser()),
        )
        ctx.support_dir = str(Path(gogdl_config_dir) / "gog-support" / ctx.game_id)
        await asyncio.to_thread(lambda: Path(ctx.support_dir).mkdir(parents=True, exist_ok=True))

    async def _install_run_gogdl_phase(
        self,
        ctx: _InstallContext,
    ) -> InstallResult | None:
        """Install run GOGDL phase."""
        languages = self._helpers.pick_languages(
            ctx.preferred_lang,
            ctx.explicit_lang,
            ctx.supported_langs,
        )
        logger.info(
            "[GOGInstaller] %s gogdl --lang=%s (preferred=%s explicit=%s "
            "supported=%s)",
            ctx.game_id,
            languages,
            ctx.preferred_lang,
            ctx.explicit_lang,
            ctx.supported_langs,
        )
        started_as_repair = ctx.install_mode == "repair"
        outcome = await self._progress_monitor.run_gogdl_with_progress(
            install_mode=ctx.install_mode,
            game_id=ctx.game_id,
            platform=ctx.platform,
            path=self._gogdl_path_for_mode(ctx),
            support_dir=ctx.support_dir,
            languages=languages,
            progress_cb=ctx.progress_cb,
        )
        if not outcome.ok and started_as_repair:
            # ``repair`` couldn't verify (e.g. the local manifest is missing or
            # stale). Don't treat the existing valid install as a failed partial
            # download — retry as a manifest-driven ``download`` that writes in
            # place. With the manifest kept (see ``_install_probe_and_prepare``)
            # this is usually a cheap "Nothing to do".
            logger.warning(
                "[GOGInstaller] repair failed for %s → retrying as download",
                ctx.game_id,
            )
            ctx.install_mode = "download"
            outcome = await self._progress_monitor.run_gogdl_with_progress(
                install_mode="download",
                game_id=ctx.game_id,
                platform=ctx.platform,
                path=ctx.base_path,
                support_dir=ctx.support_dir,
                languages=languages,
                progress_cb=ctx.progress_cb,
            )
        if not outcome.ok:
            # Never delete a pre-existing valid install just because a repair
            # (and its download fallback) failed — only clean up a partial
            # FRESH download.
            #
            # The error carries gogdl's own last lines, not a bare
            # ``download_failed``: the frontend classifier reads the message
            # text, so this is what turns a full disk or a dropped connection
            # into a translated string instead of a raw machine token.
            return self._install_failed(
                ctx.game_id,
                format_gogdl_error(outcome),
                cleanup_path=None if started_as_repair else ctx.base_path,
                cleanup_folder=None if started_as_repair else ctx.folder_name,
            )
        # NOTE: no unconditional repair pass here. gogdl ``download`` exits 0
        # only once it has written every file/chunk the manifest specifies, so
        # a clean download is already manifest-complete. The expensive
        # read-back ``repair`` now runs in ``_install_finalize`` ONLY when the
        # cheap completeness check fails — see ``_maybe_repair_and_reverify``.
        return None

    @staticmethod
    def _gogdl_path_for_mode(ctx: _InstallContext) -> str:
        """gogdl ``--path``: the base dir for a fresh download (gogdl creates the
        game folder under it), the resolved game folder for an in-place repair.
        """
        if ctx.install_mode == "download":
            return ctx.base_path
        if ctx.folder_name:
            return str(Path(ctx.base_path) / ctx.folder_name)
        return ctx.base_path

    async def _install_finalize(self, ctx: _InstallContext) -> InstallResult:
        """Install finalize."""
        found_path = await self._locate_install(
            ctx.game_id,
            ctx.base_path,
            ctx.folder_name,
            ctx.existing_dirs,
        )
        if not found_path:
            return self._install_failed(
                ctx.game_id,
                "install_not_located",
                cleanup_path=ctx.base_path,
                cleanup_folder=ctx.folder_name,
            )
        marker_ok = await self._write_install_marker(
            found_path,
            ctx.game_id,
            ctx.preferred_lang,
        )
        if not marker_ok:
            return self._install_failed(
                ctx.game_id,
                "marker_write_failed",
                cleanup_path=ctx.base_path,
                cleanup_folder=ctx.folder_name,
            )
        verification = await self._planner.verify_installation(
            ctx.game_id,
            found_path,
            ctx.platform,
            self._find_exe,
        )
        verification = await self._maybe_repair_and_reverify(
            ctx,
            found_path,
            verification,
        )
        if not verification.get("complete"):
            logger.warning(
                "[GOGInstaller] verification issue: %s",
                verification.get("issue", "unknown"),
            )
        await self._regenerate_manifest(
            ctx.game_id,
            ctx.platform,
        )
        logger.info(
            "[GOGInstaller] install complete: %s",
            found_path,
        )
        return InstallResult(
            success=True,
            store="gog",
            game_id=ctx.game_id,
            install_path=found_path,
        )

    async def _maybe_repair_and_reverify(
        self,
        ctx: _InstallContext,
        found_path: str,
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        """Run a repair pass + re-verify, but only if the install came up short.

        gogdl's ``download`` is manifest-driven and exits 0 only when complete,
        so the cheap completeness check (size-ratio / goggame.info / exe) passes
        for clean installs and we skip the expensive read-back entirely. When it
        *does* fail, the repair surfaces as a visible "Verifying…" phase (never
        silent) and we re-verify once to reflect the repaired state.
        """
        if verification.get("complete"):
            return verification
        logger.warning(
            "[GOGInstaller] verification incomplete (%s) → running repair pass",
            verification.get("issue", "unknown"),
        )
        await self._run_gogdl_repair_pass(
            ctx.game_id,
            ctx.platform,
            ctx.base_path,
            ctx.folder_name,
            ctx.preferred_lang,
            ctx.progress_cb,
        )
        return await self._planner.verify_installation(
            ctx.game_id,
            found_path,
            ctx.platform,
            self._find_exe,
        )

    async def _wipe_manifests(self, game_id: str) -> None:
        """Wipe manifests."""

        def _sync() -> None:
            """Sync."""
            base = str(Path(self._config.gogdl_config_dir).expanduser())
            parent = str(Path(base).parent)
            locations = [
                str(Path(base) / "heroic_gogdl" / "manifests" / game_id),
                str(Path(parent) / "heroic_gogdl" / "manifests" / game_id),
                str(Path(base) / "manifests" / game_id),
                str(Path(parent) / "gogdl" / "manifests" / game_id),
            ]
            for path in locations:
                if Path(path).is_file():
                    try:
                        Path(path).unlink()
                        logger.info(
                            "[GOGInstaller] cleared manifest: %s",
                            path,
                        )
                    except OSError as e:
                        logger.warning(
                            "[GOGInstaller] could not clear manifest: %s",
                            e,
                        )

        await asyncio.to_thread(_sync)

    async def _wipe_support_cache(self, game_id: str) -> None:
        """Wipe support cache."""

        def _sync() -> None:
            """Sync."""
            support_dir = str(Path(str(Path(self._config.gogdl_config_dir).expanduser())) / "gog-support" / game_id)
            if Path(support_dir).is_dir():
                try:
                    shutil.rmtree(support_dir)
                    logger.info(
                        "[GOGInstaller] cleared support cache",
                    )
                except OSError as e:
                    logger.warning(
                        "[GOGInstaller] support cleanup: %s",
                        e,
                    )

        await asyncio.to_thread(_sync)

    def _cleanup_partial(self, base_path: str, folder_name: str | None) -> None:
        """Cleanup partial."""
        if not folder_name:
            return
        partial = str(Path(base_path) / folder_name)
        if Path(partial).exists():
            logger.info(
                "[GOGInstaller] cleanup partial: %s",
                partial,
            )
            shutil.rmtree(partial, ignore_errors=True)
