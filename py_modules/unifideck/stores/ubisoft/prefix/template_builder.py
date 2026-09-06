"""
Template prefix builder — install UPC into a clean prefix.

``_TemplateBuilder`` constructs the ``.template`` prefix: a freshly
created Wine prefix with UPC pre-installed. It's used as the base for
all per-game prefixes — copying the template is much faster than
running the UPC installer every time.

Build steps:

1. ``proton run`` create-prefix to initialise a fresh prefix;
2. tweak the registry (disable mshtml, configure mountpoints);
3. run the cached UPC installer in unattended mode;
4. wait for UPC's first-launch to settle;
5. write the bootstrap marker;
6. shut UPC down gracefully.

If any step fails the partial prefix is removed and the caller gets
an explicit error code identifying the failing step.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.stores.ubisoft.binaries import UbisoftBinaryResolver

if TYPE_CHECKING:
    from unifideck.stores.ubisoft.config import UbisoftConfig
    from unifideck.stores.ubisoft.installer.cache import UbisoftInstallerCache
    from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths

    from .helpers import _PrefixHelpers
logger = logging.getLogger(__name__)

class _TemplatePrefixBuilder:
    """Template prefix builder."""

    def __init__(
        self,
        *,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
        helpers: _PrefixHelpers,
        installer_cache: UbisoftInstallerCache,
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._paths = paths
        self._helpers = helpers
        self._installer_cache = installer_cache
        self._template_task: asyncio.Task[None] | None = None

    def template_exists(self) -> bool:
        """Template exists."""
        marker = (
            Path(self._config.template_dir_expanded) / self._config.bootstrap_marker
        )
        return marker.is_file()

    def is_prefix_version_stale(self, prefix_dir: str) -> bool:
        """Check whether prefix version stale."""
        version_file = Path(prefix_dir) / "version"
        if not version_file.is_file():
            return False
        try:
            prefix_version = version_file.read_text(
                encoding="utf-8",
            ).strip()
        except OSError:
            return False
        if not prefix_version:
            return False
        family = UbisoftBinaryResolver.proton_family(
            prefix_version,
        )
        if family != "experimental":
            logger.info(
                "[UbisoftPrefixManager] prefix stale: '%s' "
                "(family=%s, expected=experimental) prefix=%s",
                prefix_version,
                family,
                prefix_dir,
            )
            return True
        return False

    @staticmethod
    def read_machine_guid(prefix_path: str) -> str:
        """Read machine guid."""
        prefix_p = Path(prefix_path)
        for reg_path in (
            prefix_p / "pfx" / "system.reg",
            prefix_p / "system.reg",
        ):
            if not reg_path.is_file():
                continue
            try:
                content = reg_path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )
            except OSError:
                continue
            match = re.search(
                r'"MachineGuid"="([^"]+)"',
                content,
            )
            if match:
                return match.group(1)
        return ""

    def queue_template_creation(self) -> None:
        """Queue template creation."""
        if self._template_task is not None and not self._template_task.done():
            logger.info(
                "[UbisoftPrefixManager] template creation already in progress",
            )
            return
        logger.info(
            "[UbisoftPrefixManager] queuing background template creation",
        )
        self._template_task = asyncio.create_task(
            self.ensure_template_prefix(),
        )

    async def regenerate_template_if_stale(self) -> None:
        """Regenerate template if stale."""
        if not self.template_exists():
            return
        template_dir = self._config.template_dir_expanded
        if not self.is_prefix_version_stale(template_dir):
            return
        logger.warning(
            "[UbisoftPrefixManager] template prefix stale, removing for recreation",
        )
        shutil.rmtree(template_dir, ignore_errors=True)

    def _auth_prefix_has_valid_credentials(self) -> bool:
        """Check whether the auth prefix holds a valid UPC credential vault."""
        auth_dir = self._config.auth_prefix_dir_expanded
        if not Path(auth_dir).is_dir():
            return False
        for _root, user_home in self._paths.iter_user_homes(
            auth_dir,
            pfx_first=True,
        ):
            css = (
                Path(user_home)
                / self._config.upc_local_subdir
                / "ConnectSecureStorage.dat"
            )
            if css.is_file() and css.stat().st_size > 100:
                return True
        return False

    async def regenerate_template_from_auth_if_diverged(self) -> None:
        """Re-derive ``.template`` from auth when identities have diverged.

        If the auth prefix holds valid credentials and the template either  (a)
        has a different ``MachineGuid``, (b) has no credentials, or (c) doesn't
        exist, remove the old template and clone a fresh one from ``.upc-auth``.
        This is the migration mechanism for already-broken installs — it only
        ever rebuilds the template FROM auth, never the reverse.
        """
        auth_dir = self._config.auth_prefix_dir_expanded
        if not await asyncio.to_thread(lambda: Path(auth_dir).is_dir()):
            return
        if not self._auth_prefix_has_valid_credentials():
            return

        template_dir = self._config.template_dir_expanded
        if self.template_exists():
            auth_guid = self.read_machine_guid(auth_dir)
            tmpl_guid = self.read_machine_guid(template_dir)
            tmpl_has_creds = self._prefix_has_valid_credentials(
                template_dir,
            )
            if auth_guid and tmpl_guid and auth_guid == tmpl_guid and tmpl_has_creds:
                return  # identities match — nothing to do
            logger.warning(
                "[UbisoftPrefixManager] template diverged from auth "
                "(auth=%s… tmpl=%s… tmpl_has_creds=%s) — realigning",
                auth_guid[:8] if auth_guid else "none",
                tmpl_guid[:8] if tmpl_guid else "none",
                tmpl_has_creds,
            )
            shutil.rmtree(template_dir, ignore_errors=True)

        logger.info(
            "[UbisoftPrefixManager] deriving template from auth prefix",
        )
        await self._helpers.create_template_from_auth_prefix(auth_dir)

    def _prefix_has_valid_credentials(self, prefix_path: str) -> bool:
        """Check whether *prefix_path* holds a valid UPC credential vault."""
        for _root, user_home in self._paths.iter_user_homes(
            prefix_path,
            pfx_first=True,
        ):
            css = (
                Path(user_home)
                / self._config.upc_local_subdir
                / "ConnectSecureStorage.dat"
            )
            if css.is_file() and css.stat().st_size > 100:
                return True
        return False

    async def ensure_template_prefix(self) -> None:
        """Ensure template prefix.

        Precedence:
        1. If ``.template`` already exists → return.
        2. If ``.upc-auth`` exists → derive template from auth  (rsync clone,
           shared ``MachineGuid`` + DPAPI identity).
        3. Else → fall back to a fresh UPC install in the template
           (pre-sign-in; will be re-derived from auth after login).
        """
        if self.template_exists():
            logger.info(
                "[UbisoftPrefixManager] template already exists",
            )
            return
        auth_dir = self._config.auth_prefix_dir_expanded
        if await asyncio.to_thread(lambda: Path(auth_dir).is_dir()):
            logger.info(
                "[UbisoftPrefixManager] deriving template from auth prefix",
            )
            await self._helpers.create_template_from_auth_prefix(auth_dir)
            return

        await self._create_template_from_fresh_install()

    async def _create_template_from_fresh_install(self) -> None:
        """Create the template via a fresh, pre-sign-in UPC install.

        Fallback path taken only when no ``.upc-auth`` prefix exists yet;
        the template will be re-derived from auth once the user logs in.
        """
        template_dir = self._config.template_dir_expanded
        logger.info(
            "[UbisoftPrefixManager] no auth prefix — creating "
            "template from fresh UPC install (pre-sign-in)",
        )
        try:
            installer_path = await self._installer_cache.ensure_cached()
            if not installer_path:
                logger.error(
                    "[UbisoftPrefixManager] installer cache "
                    "failed, aborting template creation",
                )
                return
            await asyncio.to_thread(lambda: Path(template_dir).mkdir(parents=True, exist_ok=True))
            success = await self._helpers.run_silent_installer(
                prefix_dir=template_dir,
                installer_path=installer_path,
                gameid="umu-ubisoft-template",
            )
            if not success:
                return
            if not self._paths.find_upc_exe(template_dir):
                logger.error(
                    "[UbisoftPrefixManager] upc.exe not found after template install",
                )
                return
            self._helpers.write_bootstrap_marker(
                template_dir,
                "template",
                None,
            )
            self._helpers.try_inject_auth_state([template_dir])
            logger.info(
                "[UbisoftPrefixManager] template created successfully",
            )
        except Exception:
            logger.exception("[UbisoftPrefixManager] template creation failed")
