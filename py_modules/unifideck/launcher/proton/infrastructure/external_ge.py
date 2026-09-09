"""infrastructure/external_ge.py — detection and management of external GE-Proton.

Handles third-party GE-Proton tools (such as "Proton-GE Latest" managed
by ProtonPlus, ProtonUp-Qt, or distro packages). These tools update
in-place, frequently modify ``compatibilitytool.vdf`` manifests, and may
break directory naming conventions.

**Two different questions live here, and conflating them was a real defect.**

* *Which GE do we launch with?* :func:`choose_ge` — strictly "prefer the
  newer", with an unknown version never beating a build we installed
  ourselves. Both the launcher's selector and the plugin's ProtonService
  call it, so there is one answer rather than one per call site.
* *Do we still keep our own copy on disk?* :func:`is_ge_sufficiently_fresh`
  — a tolerance, not a selection rule. It exists so ``ge_fallback`` always
  has a known-good Proton to fall back on without a live download at the
  worst possible moment.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from unifideck.launcher.proton.infrastructure import ge_installer, proton_config
from unifideck.utils import vdf_compat

logger = logging.getLogger(__name__)

EXTERNAL_GE_ALIASES: tuple[str, ...] = (
    "Proton-GE Latest",
    "Proton-GE-Latest",
    "GE-Proton-Latest",
)

#: ``config.json`` → ``compat.external_ge``. ``"auto"`` adopts an external
#: GE-Proton when one is at least as new as ours; ``"off"`` ignores external
#: tools entirely and restores pre-0.7.5 behaviour. Read here rather than at
#: the call sites so both the selector and ProtonService honour it through
#: one change.
EXTERNAL_GE_AUTO = "auto"
EXTERNAL_GE_OFF = "off"


def external_ge_enabled() -> bool:
    """True unless the user set ``compat.external_ge`` to ``"off"``."""
    return proton_config.compat_setting("external_ge", EXTERNAL_GE_AUTO).lower() != EXTERNAL_GE_OFF


def get_external_compat_roots() -> list[Path]:
    """Roots searched for externally managed compatibility tools.

    Excludes Unifideck's own compat dir — a tool we installed is by
    definition not externally managed.
    """
    return vdf_compat.compat_tool_roots(include_unifideck=False)


def parse_ge_version(tag: str) -> tuple[int, int] | None:
    """Parse major and minor from a GE-Proton tag (``GE-Proton11-5`` → ``(11, 5)``)."""
    if not tag:
        return None
    m = re.search(r"GE-Proton(\d+)[-_](\d+)", tag, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def is_ge_outdated(current_version: str, latest_version: str) -> bool:
    """True iff current_version is provably older than latest_version."""
    curr = parse_ge_version(current_version)
    latest = parse_ge_version(latest_version)
    if curr and latest:
        return curr < latest
    return False


def external_at_least_as_new(ext_version: str, cached_tag: str) -> bool:
    """True only when the external build is *provably* >= our cached build.

    Unparseable on either side returns ``False``: an unknown version is never
    grounds for preferring a Proton we did not install. That asymmetry is the
    whole point — the previous guard used ``is_ge_outdated``, which also
    returns ``False`` on a parse failure, so an unrecognised version fell
    through to "adopt the external tool". Measured on a Steam Deck: a
    ``version`` file of ``1724000000 CachyOS-11.0-100``, and a bare
    ``1724000000`` (``read_tool_internal_version`` takes ``split()[-1]``),
    both beat a newer cached ``GE-Proton11-6``.
    """
    ext = parse_ge_version(ext_version)
    cached = parse_ge_version(cached_tag)
    if not (ext and cached):
        return False
    return ext >= cached


#: How far an external build may lag the newest release before we stop
#: treating it as our only GE on disk. Not a selection rule: selection is
#: always "prefer the newer" via :func:`choose_ge`. This governs only whether
#: we *also* keep a Unifideck-managed copy as ``ge_fallback``'s recovery
#: floor, so the cost of being wrong is one redundant download, never a
#: failed launch. A whole major behind, or a version we cannot read, means
#: keep our own.
EXTERNAL_GE_MAX_MINOR_LAG = 5


def is_ge_sufficiently_fresh(
    current_version: str,
    latest_version: str,
    max_minor_lag: int = EXTERNAL_GE_MAX_MINOR_LAG,
) -> bool:
    """True iff the external build is fresh enough to be our *only* GE.

    Not a selection rule — see :data:`EXTERNAL_GE_MAX_MINOR_LAG`. True when
    the external build is newer than or equal to the latest release, or
    shares its major version and trails by at most ``max_minor_lag`` minors.
    """
    curr = parse_ge_version(current_version)
    latest = parse_ge_version(latest_version)
    if not (curr and latest):
        return False
    major_diff = latest[0] - curr[0]
    minor_diff = latest[1] - curr[1]
    if major_diff < 0:
        return True
    return major_diff == 0 and minor_diff <= max_minor_lag


def read_tool_internal_version(tool_dir: Path) -> str:
    """Read the real version tag from a Proton tool's ``version`` file."""
    for candidate_dir in (tool_dir, tool_dir.parent):
        version_file = candidate_dir / "version"
        if version_file.is_file():
            try:
                content = version_file.read_text(encoding="utf-8", errors="ignore").strip()
                if content:
                    return content.split()[-1]
            except OSError:
                pass
    if parse_ge_version(tool_dir.name):
        return tool_dir.name
    return ""


def _match_alias(tools: dict[str, Path], alias: str) -> tuple[Path, str] | None:
    """Look ``alias`` up in ``tools``, exactly first then case-insensitively.

    Returns ``(path, the name it matched under)`` or ``None``. The matched
    name is not always the alias: a case-insensitive hit reports the key as
    the manifest spells it, which is what callers pass back to Steam.
    """
    exact = tools.get(alias)
    if exact is not None:
        return exact, alias

    lowered = alias.lower()
    for name, path in tools.items():
        if name.lower() == lowered:
            return path, name
    return None


def find_external_ge_proton(
    roots: list[Path] | tuple[Path, ...] | None = None,
) -> tuple[Path, str, str] | None:
    """Find an externally managed GE-Proton installation (e.g. via ProtonPlus).

    Builds on ``vdf_compat.iter_compat_tools`` so a tool is matched by its
    manifest ``display_name`` or internal key as well as its directory name:
    ProtonPlus modifies ``compatibilitytool.vdf``, and in the equivalent
    case we already handle (``Proton-CachyOS Latest``) ``Latest`` is a
    display alias while the real directory is named something else.

    Returns ``(proton_script_path, tool_id, real_version_tag)`` or ``None``.
    """
    if not external_ge_enabled():
        return None

    scan_roots = get_external_compat_roots() if roots is None else list(roots)

    tools = vdf_compat.iter_compat_tools(scan_roots)
    if not tools:
        return None

    for alias in EXTERNAL_GE_ALIASES:
        matched = _match_alias(tools, alias)
        if matched is None:
            continue
        proton, matched_name = matched
        if ge_installer.is_proton_install_complete(proton):
            real_version = read_tool_internal_version(proton.parent)
            return proton, matched_name, real_version
    return None


@dataclass(frozen=True)
class GeChoice:
    """Which GE-Proton to launch with, and where it came from."""

    path: Path
    tool_id: str
    version: str
    is_external: bool


def choose_ge(
    external: tuple[Path, str, str] | None,
    cached_tag: str | None,
    cached_path: Path | None,
) -> GeChoice | None:
    """Pick the GE-Proton to launch with. Pure: every input is an argument.

    Prefer the external tool only when it exists **and** either we have no
    cached build of our own or it is provably at least as new as ours.
    Returns ``None`` when neither is available, so the caller falls through
    to its existing download-then-Experimental ladder unchanged.

    Both the launcher's ``_default_latest_ge`` and ``ProtonService`` call
    this, so "is the external tool the one we will actually run?" has a
    single answer. They previously used two different rules and disagreed on
    the unknown-version case.
    """
    if external is not None:
        ext_path, ext_id, ext_version = external
        if cached_path is None or external_at_least_as_new(ext_version, cached_tag or ""):
            return GeChoice(ext_path, ext_id, ext_version, is_external=True)
        logger.info(
            "[external_ge] external GE (%s) is not newer than cached %s; preferring ours",
            ext_version or "unknown", cached_tag,
        )
    if cached_path is not None and cached_tag:
        return GeChoice(cached_path, cached_tag, cached_tag, is_external=False)
    return None
