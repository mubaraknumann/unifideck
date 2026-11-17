"""launcher/proton/infrastructure/ge_repair.py — audit and repair the GE-Proton trees in use.

Run by the plugin's startup task (``ProtonService``), never on a launch.
A GE-Proton tree is shared by every game that uses it, and a damaged one
fails all of them while looking complete to every structural check. This
module finds the GE trees something actually uses, checks each against
the release it came from, and replaces what no longer matches.

Two rules bound what it touches:

* only ``GE-Proton<major>-<minor>`` directories directly in
  ``compatibilitytools.d`` that something is set to use. A tree named by an
  external manager's alias (ProtonPlus ``Proton-GE Latest``) is that
  manager's to repair.
* it never deletes a Proton. A damaged tree is replaced by a fresh copy of
  the same tag; unused trees are the user's to keep or remove. The only
  things it deletes are our own temporary download leftovers.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from unifideck.utils import vdf_compat

from . import ge_installer, ge_marker, proton_integrity, selector
from .ge_download import ProgressCb
from .ge_install_lock import install_lock

logger = logging.getLogger(__name__)

GE_DIR_RE = re.compile(r"^GE-Proton\d+-\d+$")
# ``.<tag>.dl-<random>`` staging dirs and ``.<tag>.old-<pid>`` aside trees,
# both created by ``ge_installer`` and normally removed by it.
_LEFTOVER_RE = re.compile(r"^\.GE-Proton[\w.-]*\.(?:dl|old)-\w+$")
STALE_AFTER_SECONDS = 3600
# One automatic re-download per GE tag per plugin version. Damage that comes
# back after a repair has a cause a fresh copy does not remove, and
# downloading ~600 MB on every plugin start never ends.
REPAIRS_FILE = Path("~/.local/share/unifideck/ge_repairs.json").expanduser()


@dataclass(frozen=True)
class AuditResult:
    """What the audit found for one tag.

    ``status``: ``ok`` (matches its manifest), ``verified`` (no manifest
    yet; checked against the release and kept or replaced), ``repaired``
    (was damaged; replaced or proven intact), ``unverified`` (no manifest,
    nothing found wrong, release unreachable), ``failed`` (damaged, and
    the release could not be fetched or installed), ``held`` (damaged again
    after this plugin version already repaired it once; not re-downloaded).
    """

    tag: str
    status: str
    problem: str | None = None


def ge_trees_in_use() -> list[str]:
    """GE-Proton tags in ``compatibilitytools.d`` that something will launch with.

    The managed latest, every per-game pin in ``proton_settings.json``, the
    Unifideck default, and every tool Steam's ``CompatToolMapping`` names.
    """
    names: set[str] = set()
    cached = ge_marker.read_cached_latest_tag()
    if cached:
        names.add(cached)
    names |= selector.saved_proton_tools()
    default = selector.get_unifideck_proton_tool()
    if default:
        names.add(default)
    names |= selector.steam_mapped_tools()

    compat = ge_installer.COMPAT_TOOLS_DIR
    tags: set[str] = set()
    for name in names:
        if GE_DIR_RE.match(name) and (compat / name / "proton").is_file():
            tags.add(name)
            continue
        script = vdf_compat.resolve_compat_tool(name, [compat])
        tool_dir = script.parent if script else None
        if tool_dir and tool_dir.parent == compat and GE_DIR_RE.match(tool_dir.name):
            tags.add(tool_dir.name)
    return sorted(tags)


def audit_ge(
    tag: str,
    *,
    on_repair: Callable[[str, str], None] | None = None,
    progress_cb: ProgressCb | None = None,
) -> AuditResult:
    """Check ``tag`` against its release, and re-download it if it differs.

    * manifest present and every file matches it: nothing to do;
    * damaged (marker, core DLL or manifest mismatch): ``on_repair(tag,
      problem)`` fires, then the release is downloaded, verified, and
      swapped in, or kept if it turns out to match;
    * no manifest yet: the release is downloaded once to build one, and
      the tree is kept if it matches or replaced if not.

    Runs under the install lock, so it cannot race the startup install or
    a launch-time download. Never raises.
    """
    root = ge_installer.COMPAT_TOOLS_DIR / tag
    try:
        with install_lock():
            if not (root / "proton").is_file():
                return AuditResult(tag, "ok")
            intact, problem = _inspect(root)
            if intact:
                return AuditResult(tag, "ok")
            version = _plugin_version()
            if problem and _repaired_before(tag, version):
                logger.error(
                    "[ge_repair] %s is damaged again after an automatic repair: %s. "
                    "Not downloading it again in this plugin version; launches keep using it",
                    root, problem,
                )
                return AuditResult(tag, "held", problem)
            script = _replace(tag, problem, on_repair, progress_cb)
            if script is not None and problem:
                _record_repair(tag, version, problem)
    except Exception as e:
        logger.exception("[ge_repair] audit of %s failed", tag)
        return AuditResult(tag, "failed", str(e))
    if script is None:
        if problem is None:  # healthy as far as we can tell; try again next start
            return AuditResult(tag, "unverified", "could not fetch the official release")
        return AuditResult(tag, "failed", problem)
    return AuditResult(tag, "repaired" if problem else "verified", problem)


def _inspect(root: Path) -> tuple[bool, str | None]:
    """``(intact, problem)`` for one tree.

    Intact means it carries a manifest and every file matches it; such a tree
    also gets read-only Wine libraries, which covers trees installed before
    the installer applied them. ``problem`` names the damage, or is None when
    the tree is only missing its manifest.
    """
    problem = proton_integrity.damaged_marker(root) or proton_integrity.deep_problem(root)
    if problem is None and proton_integrity.load_manifest(root) is not None:
        proton_integrity.protect_wine_libs(root)
        return True, None
    return False, problem


def _replace(
    tag: str,
    problem: str | None,
    on_repair: Callable[[str, str], None] | None,
    progress_cb: ProgressCb | None,
) -> Path | None:
    """Download ``tag``'s release and swap it in (or keep an identical tree).

    Called under the install lock. Announces a repair first when ``problem``
    is set; without one this only builds the missing manifest.
    """
    root = ge_installer.COMPAT_TOOLS_DIR / tag
    if problem:
        logger.warning("[ge_repair] %s is damaged: %s", root, problem)
        if on_repair is not None:
            on_repair(tag, problem)
    else:
        logger.info("[ge_repair] %s has no manifest; checking it against the release", root)
    release = ge_installer.fetch_release(tag)
    return ge_installer.install_release_unlocked(tag, release, progress_cb) if release else None


def _plugin_version() -> str:
    """The installed plugin's version from ``package.json``, or "" if unreadable."""
    try:
        from unifideck.core.paths import resolve_plugin_dir

        data = json.loads((resolve_plugin_dir() / "package.json").read_text(encoding="utf-8"))
        return str(data.get("version", ""))
    except Exception:
        return ""


def _repairs() -> dict[str, dict[str, str]]:
    """The automatic-repair record, ``{tag: {"version", "problem"}}``; {} if none."""
    try:
        data = json.loads(REPAIRS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _repaired_before(tag: str, version: str) -> bool:
    """Whether ``tag`` already got its automatic repair under this plugin version."""
    entry = _repairs().get(tag)
    return isinstance(entry, dict) and entry.get("version") == version


def _record_repair(tag: str, version: str, problem: str) -> None:
    """Remember that ``tag`` was repaired, so the same damage is not re-downloaded forever."""
    data = _repairs()
    data[tag] = {"version": version, "problem": problem}
    try:
        REPAIRS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = REPAIRS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(REPAIRS_FILE)
    except OSError as e:
        logger.warning("[ge_repair] could not record the repair of %s: %s", tag, e)


def _tree_bytes(root: Path) -> int:
    """Total bytes under ``root``. Never raises; unreadable entries count 0."""
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def sweep_stale_staging(now: float | None = None) -> list[Path]:
    """Delete download leftovers in ``compatibilitytools.d``; return what went.

    Only ``.GE-Proton*.dl-*`` / ``.GE-Proton*.old-*`` directories older than
    :data:`STALE_AFTER_SECONDS`: what an install interrupted by a crash or a
    reboot leaves behind (~2 GB each). Held under the install lock, so no
    install of ours is writing to one. A real tool directory never matches.
    """
    compat = ge_installer.COMPAT_TOOLS_DIR
    now = time.time() if now is None else now
    removed: list[Path] = []
    with install_lock():
        try:
            entries = list(compat.iterdir()) if compat.is_dir() else []
        except OSError as e:
            logger.warning("[ge_repair] could not scan %s: %s", compat, e)
            return removed
        for entry in entries:
            try:
                if (
                    not _LEFTOVER_RE.match(entry.name)
                    or entry.is_symlink()
                    or not entry.is_dir()
                    or now - entry.stat().st_mtime < STALE_AFTER_SECONDS
                ):
                    continue
            except OSError:
                continue
            size_mb = _tree_bytes(entry) // (1024 * 1024)
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                removed.append(entry)
                logger.info("[ge_repair] removed download leftover %s (%d MB)", entry, size_mb)
    return removed
