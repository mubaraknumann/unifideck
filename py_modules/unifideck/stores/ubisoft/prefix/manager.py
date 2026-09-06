"""
Wine prefix lifecycle manager for Ubisoft games.

``UbisoftPrefixManager`` owns the creation, validation, and destruction
of Wine prefixes used by Ubisoft games. Three categories of prefix
coexist:

1. **template prefix** (``.template``) — UPC-installed-but-no-game;
   used as the base for fresh installs (avoid running the UPC installer
   for every game).
2. **auth prefix** (``.upc-auth``) — used solely by the auth flow.
3. **per-game prefixes** — one per installed game.

The manager exposes ``ensure_template``, ``ensure_auth``, ``create_for_game``,
``destroy``, and ``validate``. Each operation is delegated to one of
``template_builder.py`` / ``auth_builder.py`` / ``helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from unifideck.stores.shared.prefix_clone import rsync_clone
from unifideck.stores.ubisoft.binaries import UbisoftBinaryResolver
from unifideck.stores.ubisoft.config import UbisoftConfig
from unifideck.stores.ubisoft.installer.cache import UbisoftInstallerCache
from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths

from .auth_builder import _AuthPrefixBuilder
from .helpers import _PrefixHelpers
from .template_builder import _TemplatePrefixBuilder

logger = logging.getLogger(__name__)

class UbisoftPrefixManager:
    """Ubisoft prefix manager."""

    def __init__(
        self,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
        binaries: UbisoftBinaryResolver,
        installer_cache: UbisoftInstallerCache,
        inject_auth_state: Callable[[list[str]], int],
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._paths = paths
        self._binaries = binaries
        self._installer_cache = installer_cache
        self._inject_auth_state = inject_auth_state
        self._helpers = _PrefixHelpers(self)
        self._template_builder = _TemplatePrefixBuilder(
            config=config,
            paths=paths,
            helpers=self._helpers,
            installer_cache=installer_cache,
        )
        self._auth_builder = _AuthPrefixBuilder(
            config=config,
            paths=paths,
            helpers=self._helpers,
            installer_cache=installer_cache,
            template_builder=self._template_builder,
        )

    def template_exists(self) -> bool:
        """Template exists."""
        return self._template_builder.template_exists()

    def is_prefix_version_stale(self, prefix_dir: str) -> bool:
        """Check whether prefix version stale."""
        return self._template_builder.is_prefix_version_stale(
            prefix_dir,
        )

    @staticmethod
    def read_machine_guid(prefix_path: str) -> str:
        """Read machine guid."""
        return _TemplatePrefixBuilder.read_machine_guid(prefix_path)

    def queue_template_creation(self) -> None:
        """Queue template creation."""
        self._template_builder.queue_template_creation()

    async def regenerate_template_if_stale(self) -> None:
        """Regenerate template if stale."""
        await self._template_builder.regenerate_template_if_stale()

    async def ensure_template_prefix(self) -> None:
        """Ensure template prefix."""
        await self._template_builder.ensure_template_prefix()

    async def ensure_auth_prefix(self) -> str | None:
        """Ensure auth prefix."""
        return await self._auth_builder.ensure_auth_prefix()

    def queue_auth_assets_ensure(
        self,
        reason: str = "background",
    ) -> None:
        """Queue auth assets ensure."""
        self._auth_builder.queue_auth_assets_ensure(reason)

    async def bootstrap_game_prefix(self, space_id: str) -> bool:
        """Bootstrap game prefix.

        Ensures a login-bearing ``.template`` shares the auth prefix's
        crypto identity before cloning — then every game prefix descends
        from the same ancestor and UPC's credential vault decrypts
        everywhere.

        When an existing prefix's ``MachineGuid`` has diverged from the
        auth prefix the method *repairs* the identity in-place via rsync
        (template → prefix with ``exclude_games=True``).  This preserves
        any installed game files under ``.../Ubisoft Game Launcher/games/``
        as well as any other user data that exists only in the target
        prefix — ``rsync -a`` without ``--delete`` never removes files.
        """
        # 1. Realign template with auth if they've diverged
        #    (migration for already-broken installs).
        await self._template_builder.regenerate_template_from_auth_if_diverged()
        # 2. Ensure the template exists (derived from auth if possible).
        if not self._template_builder.template_exists():
            await self._template_builder.ensure_template_prefix()

        auth_dir = self._config.auth_prefix_dir_expanded
        prefix_path = self._paths.get_prefix_path(space_id)
        marker_path = Path(prefix_path) / self._config.bootstrap_marker

        # 3. Existing prefix: inject credentials, or repair identity
        #    if the GUID has diverged (migration for pre-existing
        #    broken installs).
        if marker_path.is_file() and self._paths.find_upc_exe(prefix_path):
            await self._reuse_existing_game_prefix(
                space_id,
                prefix_path,
                auth_dir,
            )
            return True

        if (
            self._template_builder.template_exists()
            and await self._helpers.clone_prefix_from_template(
                space_id,
                prefix_path,
            )
        ):
            return True
        return await self._helpers.create_prefix_from_fresh_install(
            space_id,
            prefix_path,
        )

    async def _reuse_existing_game_prefix(
        self,
        space_id: str,
        prefix_path: str,
        auth_dir: str,
    ) -> None:
        """Reuse an existing game prefix: repair identity then inject creds.

        Repairs the prefix's ``MachineGuid`` from the template (via
        ``rsync`` with ``exclude_games=True``, preserving installed game
        files) when it has diverged from auth, then always injects the
        auth state — best-effort even if the repair rsync failed.
        """
        if self._game_prefix_needs_identity_repair(prefix_path, auth_dir):
            logger.warning(
                "[UbisoftPrefixManager] game prefix %s GUID "
                "diverged from auth — repairing identity "
                "from template (games preserved)",
                space_id,
            )
            # ``checksum`` is load-bearing here, not a tuning knob. This is
            # the one clone that can never delete its destination first —
            # the game's files live inside this prefix — so it is always a
            # repair over a populated tree. A ``MachineGuid`` rewrite is the
            # same length as what it replaces, and a prefix cloned with
            # ``rsync -a`` inherits its source's mtime exactly, so the
            # default size-plus-mtime quick check can skip ``system.reg``
            # and leave the identity diverged while reporting success.
            ok = await rsync_clone(
                Path(self._config.template_dir_expanded),
                Path(prefix_path),
                exclude_games=True,
                checksum=True,
            )
            if not ok:
                logger.error(
                    "[UbisoftPrefixManager] identity repair "
                    "rsync failed for %s",
                    space_id,
                )
            # Always try injection — if rsync succeeded the
            # GUIDs now match; if it failed we still report
            # best-effort state.
        self._helpers.try_inject_auth_state([prefix_path])

    def _game_prefix_needs_identity_repair(
        self,
        prefix_path: str,
        auth_dir: str,
    ) -> bool:
        """Return True if *prefix_path* needs an identity repair.

        A repair is needed when the auth prefix holds valid
        credentials, the game prefix does NOT already have its own
        working credentials, and the two prefixes have different
        ``MachineGuid`` values — which would cause the DPAPI guard
        to skip credential sync.

        A prefix that already has valid credentials (e.g. the user
        logged into UPC independently in that prefix) is left alone
        even when its GUID differs from auth.
        """
        if not Path(auth_dir).is_dir():
            return False
        if not self._template_builder._auth_prefix_has_valid_credentials():
            return False
        if self._template_builder._prefix_has_valid_credentials(prefix_path):
            return False
        auth_guid = self._template_builder.read_machine_guid(auth_dir)
        game_guid = self._template_builder.read_machine_guid(prefix_path)
        return bool(auth_guid and game_guid and auth_guid != game_guid)

    async def repair_prefix(
        self,
        space_id: str,
    ) -> bool:
        """Repair prefix."""
        prefix_path = self._paths.get_prefix_path(space_id)
        logger.info(
            "[UbisoftPrefixManager] repairing prefix for %s",
            space_id,
        )
        try:
            if await asyncio.to_thread(lambda: Path(prefix_path).is_dir()):
                shutil.rmtree(prefix_path)
                logger.info(
                    "[UbisoftPrefixManager] removed corrupted prefix for %s",
                    space_id,
                )
        except OSError:
            logger.exception("[UbisoftPrefixManager] could not remove corrupted prefix")
            return False
        return await self.bootstrap_game_prefix(space_id)
