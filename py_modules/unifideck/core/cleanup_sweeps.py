"""core/cleanup_sweeps.py — blocking filesystem sweeps for cleanup.

Pure, thread-offloaded helpers extracted from ``sync_cleanup.py`` (which had
crossed the 550-LOC volumetry cap). Each ``sweep_*`` performs one blocking
filesystem pass and returns a deleted-count; :class:`CleanupRPCMixin` calls
them via ``asyncio.to_thread``. Keeping them free of mixin state — and out of
the async methods as un-nested module functions — keeps the mixin's
per-function cognitive complexity under the gate and makes each sweep
trivially testable in isolation.

Lives in ``core/`` rather than beside its caller in ``rpc/mixins/``, where
audit §2.8 found it: this module defines no mixin, and filesystem sweep
primitives are what ``core/`` is for (``safe_delete``, ``marker_sweep`` and
``stale_installs`` are its immediate neighbours, and it calls two of them).
Sitting in the RPC leaf made ``rpc/`` look thicker than it is.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from unifideck.core.safe_delete import safe_rmtree

logger = logging.getLogger(__name__)

# Persisted credentials read by each store's ``is_available`` + stray
# auth-URL temp files left mid-flow.
_AUTH_DATA_CANDIDATES = (
    "~/.config/legendary/user.json",
    "~/.config/nile/user.json",
    "~/.config/unifideck/gog_token.json",
    "~/.config/unifideck/gogdl/gog_credentials.json",
    "~/.config/unifideck/microsoft_tokens.json",
    "~/.local/share/unifideck/microsoft_tokens.json",
    "~/.local/share/unifideck/gog_auth_url.txt",
    "~/.local/share/unifideck/ms_auth_url.txt",
    "~/.local/share/unifideck/epic_auth_url.txt",
    "~/.local/share/unifideck/amazon_auth_url.txt",
    "~/.local/share/unifideck/ubisoft_upc_session.txt",
)

# Wrapper-store id maps. Each records an absolute ``prefix_path`` per game,
# which is the only way to find a prefix the user put on an SD card — the
# data-dir wipe cannot reach those. Add a row when a wrapper store is added;
# omitting one silently leaks gigabyte-sized Wine prefixes through cleanup.
_WRAPPER_ID_MAPS = (
    "~/.local/share/unifideck/ubisoft_id_map.json",
    "~/.local/share/unifideck/battlenet_id_map.json",
)

# gogdl's build-manifest caches, all *inside* ``~/.config/unifideck``.
#
# The directory is named ``heroic_gogdl`` by gogdl itself, but it is OURS:
# ``stores/gog/tokens/gogdl_credentials.py`` points ``GOGDL_CONFIG_PATH`` at
# the parent of ``gogdl_config_dir`` (default ``~/.config/unifideck/gogdl``),
# and gogdl then creates ``heroic_gogdl/manifests/`` under it. Heroic's own
# config at ``~/.config/heroic/**`` is NEVER in scope here.
#
# Mirrors the per-game locations in ``stores/gog/install/installer.py``
# ``_wipe_manifests`` (which the per-game GOG uninstall already clears);
# ``gogdl/`` is normally gone already via :data:`_CONFIG_AUTH_DIRS`, kept
# here so the sweep is correct whichever dir the config points at.
_GOGDL_MANIFEST_DIRS = (
    "~/.config/unifideck/heroic_gogdl/manifests",
    "~/.config/unifideck/gogdl/manifests",
    "~/.config/unifideck/gogdl/heroic_gogdl/manifests",
)

# Unifideck-owned store creds under ``~/.config/unifideck`` (leaves the
# user's ``config.json`` untouched, and prunes only the *manifests* inside
# ``heroic_gogdl`` — see :data:`_GOGDL_MANIFEST_DIRS`).
_CONFIG_AUTH_FILES = (
    "gog_token.json",
    "gog_credentials.json",
    "gogdl_auth.json",
    "gog_save_paths.json",
    "microsoft_tokens.json",
)
_CONFIG_AUTH_DIRS = ("gogdl",)


def is_unifideck_owned(
    entry: dict[str, Any],
    unifideck_tag: str,
    is_unifideck_launch_options: Callable[[str], bool],
    launcher_path: str = "",
) -> bool:
    """True iff a VDF shortcut entry is Unifideck-owned.

    Ownership is decided on the ``Exe`` target, the one marker a
    foreign tool cannot forge. LaunchOptions tokens and the
    ``UNIFIDECK_TAG`` are then used to *narrow* which of our own
    entries this is — never on their own to claim one.

    Gating on those two signals alone is how "Delete all Unifideck
    data" came to delete the user's own shortcuts, and to sweep their
    grid artwork with them (the same predicate builds the artwork
    keep-set). It is the UD-006 failure mode, and adding ``battlenet``
    to ``STORE_ID_PATTERN`` widened it to every NonSteamLaunchers
    Battle.net entry — those carry a ``battlenet:<id>`` token and
    would otherwise read as ours.
    """
    from unifideck.services.shortcut.write_guard import is_ours

    if not is_ours(entry, launcher_path):
        return False
    launch = entry.get("LaunchOptions", "")
    if isinstance(launch, str) and is_unifideck_launch_options(launch):
        return True
    tags = entry.get("tags")
    tag_values: list[Any] = []
    if isinstance(tags, dict):
        tag_values = list(tags.values())
    elif isinstance(tags, list):
        tag_values = list(tags)
    return any(
        isinstance(v, str) and v == unifideck_tag for v in tag_values
    )


def _delete_entry(entry: Path) -> bool:
    """Remove one directory-entry. True when something was actually removed.

    Directories go through :func:`safe_rmtree` (structural guard); anything
    else is unlinked. Shared by the sweeps that empty a directory, so the
    file-vs-dir branching lives in one place instead of nesting inside each
    of their loops.
    """
    try:
        if entry.is_dir() and not entry.is_symlink():
            return safe_rmtree(entry)
        entry.unlink(missing_ok=True)
    except OSError:
        logger.exception("[cleanup] delete(%s) failed", entry)
        return False
    return True


def sweep_nonsteam_grid(grid_dir: str, keep_appids: set[int]) -> int:
    """Delete non-Steam grid artwork files not in *keep_appids*.

    Files are named ``<grid_dir>/<unsigned><suffix>``; real Steam
    appids are < 2³¹, so any ``>= 0x80000000`` prefix is a non-Steam
    shortcut's art. Blocking I/O — call from a thread.
    """
    prefix_re = re.compile(r"^(\d+)")
    base = Path(grid_dir)
    if not base.is_dir():
        return 0
    count = 0
    for match in base.iterdir():
        if not match.is_file():
            continue
        m = prefix_re.match(match.name)
        if not m:
            continue
        appid = int(m.group(1))
        if appid < 0x80000000 or appid in keep_appids:
            continue
        try:
            match.unlink(missing_ok=True)
            count += 1
        except OSError:
            logger.exception("[cleanup] unlink(%s) failed", match)
    return count


def sweep_auth_data() -> int:
    """Delete every store's persisted auth data + stray temp files.

    Belt-and-suspenders on top of ``registry.logout_all`` — each store's
    ``logout`` *should* clear its own credentials, but it no-ops when the
    auth submodule isn't wired and its CLI logout swallows errors. Deleting
    the files the ``is_available`` probes read guarantees signed-out state.
    """
    count = 0
    for raw in _AUTH_DATA_CANDIDATES:
        p = Path(raw).expanduser()
        try:
            if p.is_file():
                p.unlink(missing_ok=True)
                count += 1
        except OSError:
            logger.exception("[cleanup] unlink(%s) failed", p)
    return count


def sweep_data_dir(keep: frozenset[str]) -> int:
    """Delete residual state under ``~/.local/share/unifideck``.

    Iterating-and-deleting (rather than an explicit unlink list) means new
    state files added later are swept automatically — the wipe stays
    complete by construction. ``keep`` is preserved (destructive mode passes
    an empty set, reclaiming the prefixes and local saves).
    """
    data_dir = Path("~/.local/share/unifideck").expanduser()
    if not data_dir.is_dir():
        return 0
    count = 0
    for entry in data_dir.iterdir():
        if entry.name in keep:
            continue
        if _delete_entry(entry):
            count += 1
    return count


def _iter_external_prefixes(id_map: str, data_dir: str) -> Iterator[str]:
    """Yield each prefix recorded in one id map that lives outside *data_dir*.

    Split out of :func:`sweep_external_prefixes` so the parse-guard,
    shape-guard and per-entry filtering stop nesting inside its delete loop
    (that function was over the cognitive-complexity gate).
    """
    try:
        data = json.loads(
            Path(id_map).expanduser().read_text(encoding="utf-8"),
        )
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for entry in data.values():
        recorded = entry.get("prefix_path") if isinstance(entry, dict) else None
        if not recorded:
            continue
        if not str(Path(recorded).expanduser()).startswith(data_dir):
            yield str(recorded)


def sweep_external_prefixes() -> int:
    """Delete per-game prefixes recorded *outside* the data dir.

    A wrapper-store game installed to SD/custom storage records an absolute
    ``prefix_path`` in its id map that lives outside
    ``~/.local/share/unifideck/prefixes``, so the blanket data-dir wipe never
    reaches it. Internal prefixes are left to the data-dir wipe.

    Every wrapper store's id map is read, not just Ubisoft's: the id maps
    share this shape precisely so cleanup does not have to know which store
    wrote them, and a store missing from this list leaks whole Wine prefixes
    (gigabytes each) through "Delete all data".
    """
    data_dir = str(Path("~/.local/share/unifideck").expanduser())
    count = 0
    for id_map in _WRAPPER_ID_MAPS:
        for prefix in _iter_external_prefixes(id_map, data_dir):
            if safe_rmtree(prefix):
                count += 1
                logger.info("[cleanup] removed external prefix %s", prefix)
    return count


def sweep_gogdl_manifests() -> int:
    """Delete gogdl's cached build manifests. Destructive-mode only.

    GOG keeps no ``installed.json``; a manifest describes the *build* that
    is on disk, and the per-game uninstall already drops it
    (``stores/gog/install/installer.py`` ``_wipe_manifests``). "Delete all
    data" left them behind, so a wiped GOG library kept 19 manifests for
    games whose files were gone.

    Destructive-only on purpose: in non-destructive mode the game files
    stay, the manifest still describes them accurately, and dropping it
    would turn the next update into a full re-download instead of a delta.
    """
    count = 0
    for raw in _GOGDL_MANIFEST_DIRS:
        base = Path(raw).expanduser()
        if not base.is_dir():
            continue
        # The directory itself stays — gogdl reuses it on the next install.
        for entry in base.iterdir():
            if _delete_entry(entry):
                count += 1
    return count


def sweep_cache_backups(cache_dir: str) -> int:
    """Delete the ``*.json.bak`` siblings in the cache dir.

    ``CacheStore._save`` snapshots the *previous* file contents to ``.bak``
    before each write, so clearing a namespace is what creates these — a
    wipe left 291 KB of pre-wipe metadata and 125 KB of compat data sitting
    next to the emptied caches. Worse, ``CacheStore._load``'s recovery path
    restores from ``.bak`` when the live file fails to parse, so a single
    torn write after a wipe would bring the whole pre-wipe cache back.

    Must run *after* the namespaces are cleared, or the clear re-creates
    what this removed. Takes the directory from the caller (the
    CacheManager owns the path) rather than hardcoding it.
    """
    base = Path(cache_dir).expanduser()
    if not base.is_dir():
        return 0
    count = 0
    for entry in base.glob("*.json.bak"):
        try:
            entry.unlink(missing_ok=True)
            count += 1
        except OSError:
            logger.exception("[cleanup] unlink(%s) failed", entry)
    return count


def sweep_stale_install_records(drop_gog_manifests: bool) -> int:
    """Prune dangling CLI install records (+ gogdl manifests when wiping).

    One thread-offloadable seam for the two record-level sweeps that must
    run *after* ``marker_sweep.sweep_all`` — it reads legendary's and
    nile's ``installed.json`` to find the roots it sweeps, so pruning them
    any earlier would blind it.

    ``prune_dangling_records`` is self-gating (it only drops rows whose
    directory is missing), so it is safe in both cleanup modes; the gogdl
    manifests are destructive-only, hence the flag.
    """
    from unifideck.core import stale_installs

    count = len(stale_installs.prune_dangling_records())
    if drop_gog_manifests:
        count += sweep_gogdl_manifests()
    return count


def sweep_config_auth() -> int:
    """Delete Unifideck-owned store creds under ``~/.config/unifideck``.

    The live GOG refresh token sits at ``gog_credentials.json`` /
    ``gogdl_auth.json`` (top level), so a GOG login otherwise survives
    "Delete all data". Removes those plus the Unifideck gogdl config dir.
    """
    base = Path("~/.config/unifideck").expanduser()
    count = 0
    for name in _CONFIG_AUTH_FILES:
        p = base / name
        try:
            if p.is_file():
                p.unlink(missing_ok=True)
                count += 1
        except OSError:
            logger.exception("[cleanup] unlink(%s) failed", p)
    for name in _CONFIG_AUTH_DIRS:
        d = base / name
        if d.is_dir() and not d.is_symlink() and safe_rmtree(d):
            count += 1
    return count
