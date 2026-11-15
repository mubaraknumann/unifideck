"""auth.edge_browser.profile — Shared Edge auth profile management.

Extracted from edge_browser.py to isolate filesystem state concerns
(profile directory, singleton lock artifacts, cookie database,
legacy migration) from the browser process lifecycle, installer,
and CDP client.

The module is imported by ``EdgeBrowser`` which composes an
``EdgeProfileManager`` as ``self._profile`` and exposes its public
methods through delegation, preserving the pre-split public API for
``_migrate_legacy_profile``, ``cleanup_stale_profile_state``,
``has_xbox_session``, ``clear_cookies``, ``clear_profile_data``.

Responsibilities:
 - One-shot migration of the chromium-auth → edge-auth legacy profile
 - Singleton lock artifact detection + cleanup after unclean exits
 - xbox.com session cookie lookup in the Chromium cookie SQLite DB
 - Bulk cookie deletion by domain family (Xbox, MS, Live)
 - Full profile + log file erasure on explicit user request

Reference: edge_browser.py pre-split, lines 324-432 + 738-816.
"""
from __future__ import annotations

import contextlib
import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)


class EdgeProfileManager:
    """Own the shared Edge auth profile directory and its artifacts.

    All paths are passed in at construction rather than hard-coded so
    that tests can point the manager at a temporary directory. The
    ``EdgeBrowser`` composes this with the canonical module-level
    ``PROFILE_DIR`` / ``LOG_FILE`` / ``_LEGACY_*`` constants.

    Usage::

        mgr = EdgeProfileManager(
            profile_dir=PROFILE_DIR,
            log_file=LOG_FILE,
            legacy_profile_dir=_LEGACY_PROFILE_DIR,
            legacy_log_file=_LEGACY_LOG_FILE,
            cookie_domain_patterns=_MS_COOKIE_DOMAINS,
        )
        mgr.migrate_legacy_profile()
        if not mgr.has_xbox_session():
            ...
    """

    def __init__(
        self,
        *,
        profile_dir: str,
        log_file: str,
        legacy_profile_dir: str,
        legacy_log_file: str,
        cookie_domain_patterns: tuple[str, ...],
    ) -> None:
        """Store path configuration. No I/O performed here.

        Args:
          profile_dir: Current canonical profile directory path.
          log_file: Current canonical browser log file path.
          legacy_profile_dir: Pre-rename chromium-auth directory path.
          legacy_log_file: Pre-rename chromium-auth.log file path.
          cookie_domain_patterns: SQL LIKE patterns for cookies to
            clear on logout (e.g. ``('%xbox.com%', ...)``).

        """
        self.profile_dir = profile_dir
        self.log_file = log_file
        self.legacy_profile_dir = legacy_profile_dir
        self.legacy_log_file = legacy_log_file
        self.cookie_domain_patterns = cookie_domain_patterns

    # ── Legacy migration ─────────────────────────────────────────────

    def migrate_legacy_profile(self) -> None:
        """One-shot rename of chromium-auth → edge-auth profile dir.

        Users upgrading from a version prior to the Edge rename have
        their OAuth cookies and xCloud session data in
        ``~/.local/share/unifideck/chromium-auth``. Losing that would
        force them to sign in again to Microsoft, Epic, GOG, and
        Amazon all at once.

        Detects the legacy directory and, if the new one does not
        yet exist, atomically renames it. Cross-filesystem cases are
        handled gracefully via ``shutil.move`` (falls back to copy-
        then-remove on EXDEV).

        Best-effort: any failure is logged as a warning and swallowed.
        In the worst case the user has a one-time re-auth, which is
        the same outcome as not migrating.
        """
        legacy_exists = Path(self.legacy_profile_dir).is_dir()
        new_exists = Path(self.profile_dir).is_dir()
        if not legacy_exists:
            return  # nothing to migrate
        if new_exists:
            # Both exist — the user has used the refactored version
            # at least once. Leave the legacy dir alone (it's orphaned
            # but removing it could delete data the user might want
            # to keep for diagnosis).
            logger.debug(
                "[EdgeBrowser] both %s and %s exist; skipping "
                "migration",
                self.legacy_profile_dir, self.profile_dir,
            )
            return
        try:
            # shutil.move handles EXDEV gracefully (XDG data split
            # across two filesystems), unlike os.rename.
            shutil.move(self.legacy_profile_dir, self.profile_dir)
            logger.info(
                "[EdgeBrowser] migrated legacy profile %s → %s",
                self.legacy_profile_dir, self.profile_dir,
            )
        except OSError as e:
            logger.warning(
                "[EdgeBrowser] legacy profile migration failed "
                "(%s → %s): %s — users may need to re-auth",
                self.legacy_profile_dir, self.profile_dir, e,
            )
        # Same dance for the log file. Failure is silent since losing
        # the old log has no user-visible consequence.
        if (
            Path(self.legacy_log_file).is_file()
            and not Path(self.log_file).is_file()
        ):
            with contextlib.suppress(OSError):
                shutil.move(self.legacy_log_file, self.log_file)

    # ── Singleton lock artifacts ─────────────────────────────────────

    def _singleton_paths(self) -> list[str]:
        """Return singleton artifact paths for the shared auth profile."""
        profile = Path(self.profile_dir)
        return [
            str(profile / "SingletonLock"),
            str(profile / "SingletonCookie"),
            str(profile / "SingletonSocket"),
        ]

    def _has_stale_singleton_socket(self) -> bool:
        """True when the profile points at a missing singleton socket."""
        socket_path = Path(self.profile_dir) / "SingletonSocket"
        if not socket_path.is_symlink():
            return False
        try:
            target = socket_path.readlink()
        except OSError:
            return False
        return not Path(target).exists()

    def cleanup_stale_state(self) -> None:
        """Remove stale singleton artifacts after an unclean browser exit.

        Edge leaves ``Singleton*`` symlinks in the shared profile. If
        the socket target is already gone, relaunching with the same
        profile becomes unreliable and users end up deleting
        ``~/.local/share/unifideck``. Only remove these files when the
        singleton socket is clearly broken.
        """
        if not self._has_stale_singleton_socket():
            return
        removed: list[str] = []
        for path in self._singleton_paths():
            try:
                Path(path).unlink()
                removed.append(Path(path).name)
            except FileNotFoundError:
                continue
            except OSError as e:
                logger.warning(
                    "[Edge] Failed to remove stale profile "
                    "artifact %s: %s", path, e,
                )
        if removed:
            logger.info(
                "[Edge] Removed stale browser profile artifacts: %s",
                ", ".join(sorted(removed)),
            )

    # ── Cookie inspection and clearing ───────────────────────────────

    def has_xbox_session(self) -> bool:
        """True if xbox.com cookies exist in the shared browser profile.

        Returns True on error (assume logged in).
        Returns True if profile does not exist yet (no logout detected).
        """
        cookie_db = Path(self.profile_dir) / "Default" / "Cookies"
        if not cookie_db.exists():
            return True
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".db", delete=False,
            ) as tmp:
                tmp_path = tmp.name
            shutil.copy2(str(cookie_db), tmp_path)
            conn = sqlite3.connect(tmp_path, timeout=5)
            try:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM cookies "
                    "WHERE host_key LIKE '%xbox.com%'",
                )
                count = cursor.fetchone()[0]
                return cast("bool", count > 0)
            finally:
                conn.close()
        except Exception as e:
            logger.debug("[Edge] Could not read cookie DB: %s", e)
            return True
        finally:
            if tmp_path and Path(tmp_path).exists():
                Path(tmp_path).unlink()

    def clear_cookies(self) -> None:
        """Delete Xbox / Microsoft cookies from the shared profile."""
        cookie_db = Path(self.profile_dir) / "Default" / "Cookies"
        if not cookie_db.exists():
            return
        try:
            conn = sqlite3.connect(str(cookie_db), timeout=5)
            try:
                for pattern in self.cookie_domain_patterns:
                    conn.execute(
                        "DELETE FROM cookies WHERE host_key LIKE ?",
                        (pattern,),
                    )
                conn.commit()
                logger.info(
                    "[Edge] Cleared Xbox/MS cookies from shared "
                    "browser profile",
                )
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception as e:
            logger.debug(
                "[Edge] Could not clear shared browser cookies: %s", e,
            )

    def clear_cookies_for_domain(self, domain: str) -> None:
        """Delete cookies for a single domain from the shared profile.

        Opens the Edge profile's ``Default/Cookies`` SQLite DB and
        deletes the cookies of ``domain`` and its subdomains (host
        ``ea.com``, ``.ea.com``, ``signin.ea.com``; never ``idea.com``).
        Called before each OAuth flow so the user always sees a fresh
        login form (no stale session).

        Args:
            domain: the site, e.g. ``"epicgames.com"`` or ``"gog.com"``.
        """
        domain = domain.lstrip(".")
        cookie_db = Path(self.profile_dir) / "Default" / "Cookies"
        if not cookie_db.exists():
            logger.debug(
                "[Edge] No cookie DB at %s — skipping domain clear",
                cookie_db,
            )
            return
        try:
            conn = sqlite3.connect(str(cookie_db), timeout=5)
            try:
                cursor = conn.execute(
                    "DELETE FROM cookies WHERE host_key = ? OR host_key = ?"
                    " OR host_key LIKE ? ESCAPE '\\'",
                    (domain, f".{domain}", "%." + domain.replace("_", "\\_").replace("%", "\\%")),
                )
                conn.commit()
                deleted = cursor.rowcount
                logger.info(
                    "[Edge] Cleared %d %s cookie(s) from shared profile",
                    deleted, domain,
                )
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception as e:
            logger.debug(
                "[Edge] Could not clear %s cookies: %s", domain, e,
            )

    # ── Full profile wipe ────────────────────────────────────────────

    def clear_profile_data(self) -> None:
        """Delete the shared Edge auth profile and log files."""
        removed: list[str] = []
        for path in (self.profile_dir, self.log_file):
            path_obj = Path(path)
            if not path_obj.exists():
                continue
            try:
                if path_obj.is_dir() and not path_obj.is_symlink():
                    shutil.rmtree(path)
                else:
                    path_obj.unlink()
                removed.append(path_obj.name)
            except Exception as e:
                logger.warning(
                    "[Edge] Could not clear auth profile path %s: %s",
                    path, e,
                )
        if removed:
            logger.info(
                "[Edge] Cleared auth state: %s",
                ", ".join(sorted(removed)),
            )
