"""core/binaries/binary_resolver.py — Generic CLI tool locator.

Moved from core/ to core/binaries/
subpackage, grouped with binary_signatures.py (trust) and
cli_timeouts.py (run). The three form the complete lifecycle
of an external CLI tool: find it, verify it, invoke it safely.
Clean break: no shim in core/.

Replaces duplicated _find_legendary / _find_nile / gogdl resolution
in every store connector with a single 3-tier search strategy.
Strategy:
1. Explicit search paths declared in CLITool.search_paths (e.g. bundled
 binaries in `bin/`)
2. System PATH via shutil.which()
3. User-local binaries in ~/.local/bin/<name>
Only executable regular files are returned. Non-executable matches are
skipped so a read-only script file never shadows a real binary later
in PATH.
Reference: Technical Document v1.0 — Section 3.4.2 (BinaryResolver +
ExeFinder), Figure 12.
"""
import logging
import shutil
import stat
from pathlib import Path
from typing import Any

from unifideck.core.types.domain import CLITool

from .binary_signatures import verify_bundled_binary

logger = logging.getLogger(__name__)


def _log_signature_mismatch(name: str, path: str) -> None:
    """Log loudly when a bundled binary does not match its declared hash.

    FAIL-OPEN by design: this reports, it does not block. A mismatch means
    the file on disk is not the version the manifest pins — a half-applied
    update, a hand-copied binary, a user-swapped one — and the symptoms
    (unknown CLI flags, unparsable output) are otherwise attributed to the
    store rather than to the binary. One ERROR line in the log turns days
    of misdirected triage into a one-line answer.

    Refusing to run instead would be worse: an over-strict check that
    bricks every store on a hash the maintainer forgot to bump is a far
    likelier outcome than a maliciously swapped binary on a Deck where the
    user already owns the plugin directory.

    Only Tier-1 (bundled) hits are checked. A PATH or ~/.local/bin binary
    is deliberately the user's own and has no expected hash.
    """
    verdict = verify_bundled_binary(name, path)
    if verdict is False:
        logger.error(
            "[BinaryResolver] %s at %s does NOT match its declared SHA256 "
            "— it is not the version package.json pins. Store failures "
            "from here are most likely this, not the store. Reinstall the "
            "plugin to restore the bundled binary.",
            name, path,
        )


def _is_executable(path: str) -> bool:
    """Return True if path is a regular file with the user-execute bit set."""
    try:
        st = Path(path).stat()
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    return bool(st.st_mode & stat.S_IXUSR)


class BinaryResolver:
    """Generic CLI tool locator shared across all stores.

    Usage::

        resolver = BinaryResolver(config=cfg)
        path = resolver.resolve(
            CLITool("legendary", ["bin/legendary"]),
        )
    """

    def __init__(self, config: Any | None = None) -> None:
        # The version-check timeout is the only tunable knob;
        # loaded once at init so we don't pay the config lookup
        # on every resolve() call. ``config`` is duck-typed —
        # any object with a ``.get(str) -> str | int`` method
        # works; we use ``Any`` rather than constraining to
        # ConfigManager to keep this module free of upstream
        # dependencies for testing.
        del config  # no per-resolver config knobs remain (see item 44)

    def resolve(self, tool: CLITool) -> str | None:
        """Locate the binary for a CLI tool.

        Args:
          tool: Descriptor with name, search_paths, etc.

        Returns:
          Absolute path to an executable binary, or None if
          not found.

        """
        # Tier 1 — explicit search paths (bundled or hardcoded)
        for candidate in tool.search_paths:
            expanded = str(Path(candidate).expanduser())
            if (
                Path(expanded).is_absolute()
                and _is_executable(expanded)
            ):
                logger.debug(
                    "[BinaryResolver] %s found in search_paths: "
                    "%s",
                    tool.name, expanded,
                )
                _log_signature_mismatch(tool.name, expanded)
                return expanded

        # Tier 2 — system PATH
        which = shutil.which(tool.name)
        if which and _is_executable(which):
            logger.debug(
                "[BinaryResolver] %s found in PATH: %s",
                tool.name, which,
            )
            return which

        # Tier 3 — user-local bin
        local = Path.home() / ".local" / "bin" / tool.name
        if _is_executable(str(local)):
            logger.debug(
                "[BinaryResolver] %s found in ~/.local/bin",
                tool.name,
            )
            return str(local)

        logger.info(
            "[BinaryResolver] %s not found in any tier",
            tool.name,
        )
        return None

    # There is deliberately no ``check_version``. It existed with zero
    # callers — the only apparent call site was inside this class's own
    # ``Usage::`` docstring block — and ``CLITool.min_version`` was never
    # compared to anything, for any store. So the "version check" the audit
    # credited Epic and Amazon with (and GOG with bypassing) never existed.
    #
    # Deleted rather than built, because a version string is the weaker of
    # two guarantees the project already has: bundled binaries are pinned by
    # SHA-256 in ``package.json``'s ``remote_binary`` and verified by
    # ``binary_signatures``. And the one real version constraint in the tree
    # is a **maximum** — nile is pinned at 1.1.2 because 1.2.0 deletes the
    # ``user.json`` the Amazon reader needs — which ``min_version`` could not
    # have expressed anyway. Audit register item 44.


# Singleton instance — shared across all stores
binary_resolver = BinaryResolver()
