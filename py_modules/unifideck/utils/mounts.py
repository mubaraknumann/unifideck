"""utils/mounts.py — shared external-storage mount enumeration.

Centralizes ``/proc/mounts`` scanning that was independently
duplicated in ``rpc/mixins/storage.py``, ``rpc/mixins/download.py``,
and ``utils/paths.py``. The three copies had already drifted:
``download.py``'s skip-list was missing the virtual-prefix check
entirely, ``paths.py``'s skip-list included ``"autofs"`` the other
two didn't, and ``storage.py``'s eligibility check called
``Path.is_dir()`` uncaught — which *raises* ``PermissionError``
(not just returns ``False``) on a mount root is denied access to,
per CPython's ``pathlib`` implementation (which only swallows
``ENOENT``/``ENOTDIR``/``EBADF``/``ELOOP``, not ``EACCES``).

FUSE-mounted external media (NTFS via ntfs-3g, exFAT on some
distros/kernels) are, by FUSE's own security design, invisible to
every UID except the one that mounted them — unless the mount used
``allow_other``, which requires ``user_allow_other`` in
``/etc/fuse.conf`` and is disabled by default.

So whenever the effective UID of this process differs from the UID that
mounted the media, we can neither stat nor validate those mounts: they
need a one-off subprocess demoted to the mount's owning uid, parsed
from the ``uid=``/``gid=`` options ntfs-3g/fuse-exfat mounts carry.
Never ``os.setuid()``/``os.seteuid()`` in this process directly —
glibc's ``setuid()`` is process-wide (synchronized across every
thread), which would be unsafe next to other concurrent async/thread
work in this same backend.

Historical note: this file used to state that the backend "runs as
root (Decky's ``plugin_loader.service`` sets ``User=root``)". That is
no longer true and the demotion logic here does not depend on it —
Decky Loader v3 drops privileges for plugins without the ``root``
flag, and ``plugin.json`` does not set it, so the backend runs as the
desktop user. The ``uid``-demoted probe is still correct and still
needed: it is keyed off the *mount's* owning uid versus ours, which
can differ either way. The support bundle's ``identity`` block records
the live uid/euid, so a future reader can check rather than assume.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from unifideck.utils.mount_naming import (
    BY_UUID_DIR,
    device_uuid,
    legacy_mount_id,
    unescape_mount_field,
    uuid_by_device,
)

logger = logging.getLogger(__name__)

# Filesystem types that should never be offered as install/scan
# targets. Deliberately does NOT include real on-disk filesystems
# external media commonly uses (exfat, ntfs3, fuseblk, vfat, btrfs,
# xfs, ext4) — only virtual/pseudo mounts belong here.
SKIP_FSTYPES = frozenset({
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup",
    "cgroup2", "pstore", "bpf", "debugfs", "tracefs", "hugetlbfs",
    "ramfs", "overlay", "squashfs", "fuse.gvfsd-fuse",
    "fuse.portal", "securityfs", "configfs", "efivarfs", "mqueue",
    "autofs",
})

VIRTUAL_PREFIXES = ("/dev/", "/sys/", "/proc/", "/run/user/")

# Where the desktop session automounts removable media. A mount under
# one of these is something the *user* plugged in, so refusing it is
# worth a warning; refusing a system mount is routine and stays debug.
AUTOMOUNT_ROOTS = ("/run/media/", "/media/")

_DEMOTE_TIMEOUT = 5.0


@dataclass(frozen=True)
class MountInfo:
    """One eligible, accessible external mount.

    ``effective_uid``/``effective_gid`` are ``None`` when root has
    direct access; otherwise every further filesystem operation on
    this mount (subdir creation, deeper scans) must also go through
    a demoted subprocess — the FUSE permission wall applies to every
    operation, not just the initial probe.

    ``uuid`` is the filesystem's superblock UUID when udev exposes one.
    It is this mount's durable identity: ``mount_point`` changes the
    moment the user relabels the drive, and ``st_dev`` is a kernel
    device number that shifts with USB enumeration order.
    """

    device: str
    mount_point: str
    fstype: str
    st_dev: int
    options: dict[str, str]
    writable: bool
    effective_uid: int | None = None
    effective_gid: int | None = None
    uuid: str = ""


def is_user_media(mount_point: str) -> bool:
    """True if *mount_point* is under a desktop automount root."""
    return mount_point.startswith(AUTOMOUNT_ROOTS)


def parse_mount_options(raw: str) -> dict[str, str]:
    """Parse a ``/proc/mounts`` option field into a dict.

    Bare flags (no ``=``) map to ``""``. Never raises — malformed
    input just yields fewer entries.
    """
    result: dict[str, str] = {}
    for item in raw.split(","):
        if not item:
            continue
        key, _, value = item.partition("=")
        result[key] = value
    return result


def is_eligible_type(fstype: str, mount_point: str) -> bool:
    """True if *mount_point*/*fstype* could be a real storage device.

    Pure — no filesystem access, never raises. Deliberately kept
    separate from any ``is_dir()``/``stat()`` call: bundling them (as
    the old per-file copies did) meant a permission-denied FUSE mount
    raised out of the "is this even eligible" check instead of being
    routed through the demotion fallback below.
    """
    return fstype not in SKIP_FSTYPES and not mount_point.startswith(VIRTUAL_PREFIXES)


def stat_dev(path: str) -> int:
    """Return ``st_dev`` for *path*, or 0 if it can't be stat'd directly."""
    try:
        return Path(path).stat().st_dev
    except OSError:
        return 0


def run_demoted(
    argv: list[str], uid: int, gid: int | None = None, *, timeout: float = _DEMOTE_TIMEOUT,
) -> subprocess.CompletedProcess[str] | None:
    """Run *argv* as *uid* (and optionally *gid*); ``None`` on any failure.

    Used to reach FUSE mounts that were mounted by, and are only
    visible to, a different uid than this process's. Spawns a
    genuinely separate, demoted subprocess rather than changing this
    process's own identity — see the module docstring for why that
    matters in a shared async backend.
    """
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
            user=uid, group=gid,
        )
    except Exception as e:
        logger.debug("[mounts] demoted run %s as uid=%s failed: %s", argv, uid, e)
        return None


def mount_id(m: MountInfo) -> str:
    """Durable id for one mount — its filesystem UUID where possible.

    Deriving the id from the mount point (the old scheme, kept in
    ``legacy_mount_id``) made identity depend on the drive's label: two
    drives labelled "External SSD" and "External_SSD" collapsed onto one
    id, because the label was sanitized into it. Worse, the tiebreak was
    ``st_dev``, so which drive kept the bare id depended on
    ``/proc/mounts`` order — a replug could silently repoint a saved
    default at the other drive. A superblock UUID survives relabelling,
    replugging and reboots, and cannot collide by naming.
    """
    if m.uuid:
        return f"ext:{m.uuid}"
    return legacy_mount_id(m.mount_point)


def is_sdcard_source(device: str) -> bool:
    """True if *device* looks like the Deck's microSD card slot."""
    return device.startswith("/dev/mmcblk0")


def assign_unique_ids(mounts: list[MountInfo]) -> list[tuple[str, MountInfo]]:
    """Pair each mount with a UNIQUE ``ext:`` id, in input order.

    ``mount_id`` returns a UUID-based id, which cannot collide by naming
    — but two mounts can still land on one id: a cloned filesystem
    (``dd``) genuinely repeats its UUID, and mounts with no UUID at all
    fall back to a basename that two devices can share (e.g.
    ``/run/media/deck/GAMES`` and ``/media/GAMES``). Either way a
    duplicate id means duplicate picker rows (React key clashes) and an
    ambiguous install-target lookup, so on collision the later device is
    disambiguated deterministically with its ``st_dev`` (a counter when
    ``st_dev`` is 0). BOTH the enumerator (storage) and the resolver
    (download) iterate the same ordered, deduped list, so they derive
    the same id. The first/only device with a given base keeps the bare
    id.
    """
    seen: dict[str, int] = {}
    result: list[tuple[str, MountInfo]] = []
    for m in mounts:
        base = mount_id(m)
        count = seen.get(base, 0)
        seen[base] = count + 1
        if count == 0:
            result.append((base, m))
        else:
            suffix = m.st_dev if m.st_dev else count
            result.append((f"{base}-{suffix}", m))
    return result


def dedupe_by_device(mounts: list[MountInfo]) -> list[MountInfo]:
    """Collapse mounts that share a nonzero ``st_dev`` (same physical device).

    Entries with ``st_dev == 0`` (undeterminable even via a demoted
    retry) are never collapsed against each other — rare in practice,
    and safer than risking a false merge of two distinct devices onto
    one row.
    """
    seen: set[int] = set()
    result: list[MountInfo] = []
    for m in mounts:
        if m.st_dev != 0:
            if m.st_dev in seen:
                logger.debug(
                    "[mounts] duplicate device st_dev=%s at %s — already counted",
                    m.st_dev, m.mount_point,
                )
                continue
            seen.add(m.st_dev)
        result.append(m)
    return result


def ensure_games_subdir(
    mount_point: str, effective_uid: int | None, effective_gid: int | None = None,
) -> str:
    """Return ``<mount_point>/Games`` (created if needed), or *mount_point* on failure.

    Created directly when *effective_uid* is ``None`` (root already
    has access — today's fast path, unchanged). Otherwise created via
    a demoted subprocess so the directory ends up owned by the
    desktop user, not root — a root-owned directory inside a user's
    FUSE-mounted drive would be unmanageable from their own desktop
    session.
    """
    games = Path(mount_point) / "Games"
    if effective_uid is None:
        try:
            games.mkdir(parents=True, exist_ok=True)
            return str(games)
        except OSError as e:
            logger.debug("[mounts] mkdir %s failed: %s", games, e)
            return mount_point
    proc = run_demoted(["mkdir", "-p", str(games)], effective_uid, effective_gid)
    if proc is not None and proc.returncode == 0:
        return str(games)
    logger.debug("[mounts] demoted mkdir %s as uid=%s failed", games, effective_uid)
    return mount_point


def mount_is_dir(path: str, effective_uid: int | None) -> bool:
    """Demotion-aware ``is_dir()`` — see module docstring for why."""
    if effective_uid is None:
        try:
            return Path(path).is_dir()
        except OSError:
            return False
    proc = run_demoted(["test", "-d", path], effective_uid)
    return proc is not None and proc.returncode == 0


def mount_child_dirs(path: str, effective_uid: int | None) -> list[Path]:
    """Immediate child directories of *path*, symlinks excluded.

    Demotion-aware: a FUSE mount only visible via a demoted uid for
    *enumeration* is still invisible to root for every further
    operation, including listing its children.
    """
    if effective_uid is None:
        found: list[Path] = []
        try:
            for child in Path(path).iterdir():
                if child.is_dir() and not child.is_symlink():
                    found.append(child)
        except OSError:
            pass
        return found
    proc = run_demoted(["find", path, "-mindepth", "1", "-maxdepth", "1", "-type", "d"], effective_uid)
    if proc is None or proc.returncode != 0:
        return []
    return [Path(line) for line in proc.stdout.splitlines() if line]


def _parse_uid(raw: str | None) -> int | None:
    """Parse a mount option's uid/gid value; ``None`` if absent/malformed/self."""
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value != os.geteuid() else None


def _probe_direct(mount_point: str) -> tuple[bool, bool]:
    """Root-direct ``(is_dir, writable)`` — never raises."""
    try:
        is_dir = Path(mount_point).is_dir()
    except OSError:
        return False, False
    if not is_dir:
        return False, False
    try:
        writable = os.access(mount_point, os.W_OK)
    except OSError:
        writable = False
    return True, writable


def _probe_demoted(mount_point: str, uid: int, gid: int | None) -> tuple[bool, bool]:
    """Demoted ``(is_dir, writable)`` via subprocess — never raises."""
    is_dir = run_demoted(["test", "-d", mount_point], uid, gid)
    if is_dir is None or is_dir.returncode != 0:
        return False, False
    writable = run_demoted(["test", "-w", mount_point], uid, gid)
    return True, bool(writable is not None and writable.returncode == 0)


def _probe_stat_dev(mount_point: str, uid: int, gid: int | None) -> int:
    proc = run_demoted(["stat", "-c", "%d", mount_point], uid, gid)
    if proc is None or proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return 0


def _printable(path: str) -> str:
    """*path* safe to log: undecodable label bytes shown as ``\\xNN`` escapes.

    ``scan_mounts`` decodes with surrogateescape, so a non-UTF-8 label
    carries lone surrogates. A UTF-8 log handler cannot encode those and
    drops the whole line, which is the warning that explains a missing drive.
    """
    return path.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def _log_skip(mp: str, fstype: str, reason: str) -> None:
    """Log why a mount was refused.

    Refusing user-plugged media is the whole "drive not detected" failure,
    so say so at a level that reaches the log file — these were debug-only,
    which is why five session logs in the first report of this bug mentioned
    it exactly zero times.
    """
    level = logging.WARNING if is_user_media(mp) else logging.DEBUG
    logger.log(level, "[mounts] skip %s (%s): %s", _printable(mp), fstype, reason)


def _resolve_mount(
    device: str, mp: str, fstype: str, raw_options: str, require_writable: bool,
    uuid: str = "",
) -> MountInfo | None:
    """Build a ``MountInfo`` for one already-eligible-type mount line, or ``None``."""
    shown = _printable(mp)
    options = parse_mount_options(raw_options)
    uid = _parse_uid(options.get("uid"))
    gid = _parse_uid(options.get("gid")) if uid is not None else None

    is_dir, writable = _probe_direct(mp)
    effective_uid: int | None = None
    effective_gid: int | None = None
    st_dev = stat_dev(mp) if is_dir else 0

    if not is_dir and uid is not None:
        is_dir, writable = _probe_demoted(mp, uid, gid)
        if is_dir:
            st_dev = _probe_stat_dev(mp, uid, gid)
            effective_uid, effective_gid = uid, gid
            logger.info(
                "[mounts] %s (%s) accessible only via demoted uid=%s "
                "(FUSE mount, no allow_other)", shown, fstype, uid,
            )
        else:
            logger.debug(
                "[mounts] %s (%s) inaccessible even as uid=%s after demoted retry",
                shown, fstype, uid,
            )

    if not is_dir:
        _log_skip(mp, fstype, "not an accessible directory")
        return None
    if require_writable and not writable:
        _log_skip(mp, fstype, f"not writable by uid {os.geteuid()}")
        return None

    return MountInfo(
        device=device, mount_point=mp, fstype=fstype, st_dev=st_dev,
        options=options, writable=writable,
        effective_uid=effective_uid, effective_gid=effective_gid,
        uuid=uuid,
    )


def scan_mounts(
    home_dev: int,
    *,
    mounts_path: str | os.PathLike[str] = "/proc/mounts",
    require_writable: bool = True,
    by_uuid_dir: Path = BY_UUID_DIR,
) -> list[MountInfo]:
    """Enumerate eligible external mounts from *mounts_path*.

    Returns every mount that is (a) not a virtual/system fstype,
    (b) not the device hosting ``$HOME``, and (c) actually
    accessible — either directly (root-owned/native filesystems) or,
    for FUSE mounts owned by another uid, via a one-off demoted
    subprocess. ``require_writable=False`` accepts a
    readable-but-not-writable mount (``paths.py``'s use case —
    discovering already-installed games, a read-only concern); the
    default ``True`` matches ``storage.py``/``download.py``'s need
    to offer only genuinely usable install targets.

    Does not dedupe by device — see ``dedupe_by_device``, an opt-in
    step only ``storage.py`` needs (one row per physical device).

    *by_uuid_dir* exists as a seam for tests, which must not have the
    host's real UUID index bleed into fixtures whose device names
    happen to match the machine they run on.
    """
    found: list[MountInfo] = []
    try:
        # Bytes + os.fsdecode, not read_text(): a filesystem label that
        # isn't valid UTF-8 (an old Latin-1 NTFS/exFAT label) makes
        # read_text() raise UnicodeDecodeError — a ValueError, which the
        # except clause below does NOT catch, so it propagated out of
        # get_storage_locations and the picker lost *every* location,
        # internal included. surrogateescape round-trips those bytes
        # back out through every later Path call.
        lines = os.fsdecode(Path(mounts_path).read_bytes()).splitlines()
    except OSError as e:
        logger.debug("[mounts] %s read failed: %s", mounts_path, e)
        return found

    # One readlink sweep for the whole scan, not one per mount.
    uuids = uuid_by_device(by_uuid_dir)

    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        # Escapes must be decoded AFTER splitting on whitespace: the
        # escaping is what keeps a spaced mount point one field.
        device, mp, fstype, raw_options = (
            unescape_mount_field(field) for field in parts[:4]
        )
        if not is_eligible_type(fstype, mp):
            continue
        info = _resolve_mount(
            device, mp, fstype, raw_options, require_writable,
            device_uuid(device, uuids),
        )
        if info is None:
            continue
        if info.st_dev != 0 and info.st_dev == home_dev:
            continue
        found.append(info)
    return found
