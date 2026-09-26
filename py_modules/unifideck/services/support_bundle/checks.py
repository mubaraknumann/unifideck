"""support_bundle/checks.py — Derived verdicts.

Turns the audit and the environment report into ``PASS`` / ``FAIL`` /
``WARN`` / ``N/A`` lines rendered at the top of ``diagnostics.txt``, so
a support engineer reads conclusions first and drills into the raw
tables only when a verdict is surprising.

Every check is derived from data already collected — none of them
touches the filesystem again — and each one exists because a specific
failure class was expensive to diagnose without it. The triangulation
check is the clearest example: comparing shortcut counts across the
three places they are recorded *is* the "synced but zero games" bug,
and establishing it currently takes three separate file requests.

Checks are **read-only and non-throwing**. :func:`run_checks` wraps
each one, so a check that trips over unexpected data reports ``error``
for itself and the rest still run.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import counts as counts_mod
from .check_kit import View as _View
from .check_kit import fail as _fail
from .check_kit import na as _na
from .check_kit import ok as _ok
from .check_kit import warn as _warn
from .checks_protontricks import check_protontricks
from .checks_shortcuts import check_shortcut_backups, check_shortcut_ownership_census
from .probe_storage import RISKY_FSTYPES, is_user_storage
from .spec import BundleContext, CheckResult, PathRecord

logger = logging.getLogger(__name__)

_LOW_DISK_BYTES = 2 * 1024 * 1024 * 1024
_STALE_CACHE_HOURS = 24 * 30
_CA_STORE_MAX_AGE_DAYS = 365 * 2
_NON_STEAM_APPID_BASE = 2000000000
_STORE_TOKEN_KEYS = {
    "gog": "gog_token",
    "microsoft": "microsoft_token",
    "epic": "legendary_user",
    "amazon": "nile_user",
    "itch": "butler_db",
}


def _check_not_root(view: _View) -> CheckResult:
    name = "backend_runs_as_desktop_user"
    identity = view.block("identity")
    ids = identity.get("ids") or {}
    if not ids:
        return _na(name, "could not read process ids")
    if identity.get("running_as_root"):
        return _warn(name, f"euid 0 - writes may land with root ownership: {ids}")
    return _ok(name, f"uid/euid {ids.get('uid')}/{ids.get('euid')}")


def _check_shortcuts_vdf(view: _View) -> CheckResult:
    name = "shortcuts_vdf_present_and_executable"
    record = view.by_key.get("shortcuts_vdf")
    if record is None or record.status == "root_unresolved":
        return _na(name, "shortcuts.vdf path not resolved by the plugin")
    if not view.present("shortcuts_vdf"):
        return _fail(name, f"shortcuts.vdf {view.status('shortcuts_vdf')}")
    mode = record.mode or ""
    # An empty shortcuts.vdf used to report PASS, because ``present()``
    # accepts the "empty" status too. That is the single loudest symptom
    # of the library-wipe this check exists to catch, so it fails first.
    if not record.size:
        return _fail(
            name,
            "shortcuts.vdf is zero bytes - every non-Steam shortcut, ours "
            "and the user's, is gone. Restore from shortcuts_backups/",
        )
    # Test the actual permission bit rather than string-matching "644":
    # 0o600 and 0o444 disarm NonSteamLaunchers' scanner sentinel exactly
    # as 0o644 does, and used to pass silently.
    if mode and not mode.endswith(("1", "3", "5", "7")):
        return _warn(
            name,
            f"mode {mode} is not executable: the exec bit has been lost "
            "before, and an external tool then wiped library entries",
        )
    return _ok(name, f"mode {mode}, {record.size} bytes")


def _check_triangulation(view: _View) -> CheckResult:
    """Surface the three shortcut counts; assert only the clear case.

    An earlier version failed whenever the registry held more entries
    than ``shortcuts.vdf``, which is normal: the registry is keyed by
    ``store:game_id`` and remembers every game it has ever created a
    shortcut for, while the vdf holds only the live ones. On a healthy
    device that produced a red FAIL, and a check that cries wolf costs
    more trust than it earns across the other twenty-two.

    The one unambiguous symptom is "the plugin thinks it created
    shortcuts and Steam has none" — that is the 'synced but zero games'
    report. Everything else is reported as context for a human.
    """
    name = "shortcut_count_triangulation"
    counts = counts_mod.shortcut_counts(view.by_key)
    known = {key: value for key, value in counts.items() if value is not None}
    if len(known) < 2:
        return _na(name, f"not enough sources to compare: {counts}")
    registry = counts.get("registry") or 0
    in_vdf = counts.get("shortcuts_vdf")
    if in_vdf == 0 and registry > 0:
        return _fail(
            name,
            f"the plugin has {registry} shortcuts recorded but shortcuts.vdf "
            f"contains none - this is the 'synced but no games appear' "
            f"symptom (counts: {counts})",
        )
    return _ok(
        name,
        f"{counts} - these are not expected to match: the registry "
        "remembers every game ever synced, the vdf holds live shortcuts, "
        "and games.map is the launcher manifest",
    )


def _check_steam_root(view: _View) -> CheckResult:
    name = "steam_root_is_live"
    steam = view.block("steam")
    root = steam.get("root")
    if not root:
        return _fail(name, "no Steam root resolved")
    active = steam.get("active_user") or {}
    if not active.get("resolved"):
        return _warn(
            name,
            f"root {root} resolved but no active user id "
            f"(userdata dirs: {active.get('userdata_dirs')})",
        )
    return _ok(name, f"{root} [{steam.get('root_source')}] user {active['resolved']}")


def _check_launcher_binary(view: _View) -> CheckResult:
    name = "launcher_binary_executable"
    plugin = view.block("plugin")
    if not plugin.get("resolved"):
        return _na(name, "plugin install directory not resolved")
    entry = (plugin.get("binaries") or {}).get("unifideck-launcher") or {}
    if not entry.get("present"):
        return _fail(name, "bin/unifideck-launcher missing - no shortcut can launch")
    if not entry.get("executable"):
        return _fail(name, f"present but not executable (mode {entry.get('mode')})")
    return _ok(name, f"mode {entry.get('mode')}")


def _check_store_binaries(view: _View) -> CheckResult:
    name = "store_cli_binaries"
    binaries = view.block("plugin").get("binaries") or {}
    if not binaries:
        return _na(name, "plugin dir not resolved")
    broken = [
        f"{cli}({'missing' if not info.get('present') else 'not executable'})"
        for cli, info in binaries.items()
        if cli in ("legendary", "gogdl", "nile", "comet")
        and not (info.get("present") and info.get("executable"))
    ]
    if broken:
        return _fail(name, f"unusable: {', '.join(broken)}")
    return _ok(name, "legendary, gogdl, nile, comet all present and executable")


def _check_umu_runtime(view: _View) -> CheckResult:
    name = "umu_runtime_complete"
    variants = view.block("runtime").get("umu_variants") or []
    if not variants:
        return _na(name, "no umu runtime downloaded yet")
    broken = [item["variant"] for item in variants if not item.get("complete")]
    if broken:
        return _fail(
            name,
            f"{', '.join(broken)} missing its entry point - umu reports "
            "'up to date' and then fails without triggering self-heal",
        )
    return _ok(name, f"{len(variants)} variant(s) complete")


def _check_data_dir_writable(view: _View) -> CheckResult:
    name = "data_dir_writable"
    import os

    data = view.ctx.root("data")
    if not data:
        return _fail(name, "data dir not resolved")
    if not os.access(data, os.W_OK | os.X_OK):
        return _fail(name, f"{data} is not writable - all state writes will fail")
    return _ok(name, data)


def _check_auth_consistency(view: _View) -> CheckResult:
    name = "auth_state_consistency"
    present = [
        store for store, key in _STORE_TOKEN_KEYS.items()
        if view.present(key)
    ]
    if not present:
        return _na(name, "no store credentials on disk")
    return _ok(name, f"credentials present for: {', '.join(sorted(present))}")


def _check_disk_space(view: _View) -> CheckResult:
    name = "disk_space"
    locations = view.block("storage").get("install_locations") or []
    low = [
        f"{item['path']} ({_gb(item.get('free_bytes'))} free)"
        for item in locations
        if isinstance(item.get("free_bytes"), int)
        and item["free_bytes"] < _LOW_DISK_BYTES
    ]
    if low:
        return _warn(name, f"low space: {'; '.join(low)}")
    return _ok(name, f"{len(locations)} install location(s) have headroom")


def _gb(value: Any) -> str:
    """Format a byte count in GB for a one-line verdict."""
    if not isinstance(value, int):
        return "unknown"
    return f"{value / (1024 ** 3):.1f}G"


def _check_install_fstype(view: _View) -> CheckResult:
    name = "install_location_fstype"
    locations = view.block("storage").get("install_locations") or []
    risky = [
        f"{item['path']} ({item.get('fstype')})"
        for item in locations if item.get("risky_fstype")
    ]
    if risky:
        return _warn(
            name,
            f"on a filesystem that cannot host a Wine prefix reliably: "
            f"{'; '.join(risky)} (risky: {sorted(RISKY_FSTYPES)})",
        )
    return _ok(name, "all install locations on POSIX filesystems")


def _check_storage_visibility(view: _View) -> CheckResult:
    """Flag user storage the kernel sees but the install picker won't offer.

    Scoped to removable, network and automounted media via
    :func:`is_user_storage`. An earlier version compared every mounted
    device and failed on every healthy machine, because the internal
    disk is bind-mounted at paths the plugin's install-target scanner
    deliberately filters out.

    ``offered_in_picker`` is the field that matters, not
    ``visible_to_plugin``: the user's complaint is "I can't install
    there", and a mount can be perfectly visible to the scanner yet
    still be refused by the picker's writability gate. Each device
    carries its own ``visibility_note`` saying which of the two it is.
    """
    name = "storage_visible_to_plugin"
    devices = view.block("storage").get("devices") or []
    candidates = [item for item in devices if is_user_storage(item)]
    unusable = [
        f"{item['name']} at {item['mounted_at']} - {item['visibility_note']}"
        for item in candidates
        if item.get("mounted_at") and not item.get("offered_in_picker")
    ]
    if unusable:
        return _fail(
            name,
            "removable/external storage is mounted but the plugin cannot "
            f"install to it (this is the 'drive not detected' failure): "
            f"{'; '.join(unusable)}",
        )
    if not candidates:
        return _na(name, "no removable or external storage attached")
    return _ok(name, f"{len(candidates)} external device(s) offered by the picker")


def _check_unmounted_removable(view: _View) -> CheckResult:
    name = "unmounted_removable_present"
    devices = view.block("storage").get("devices") or []
    idle = [
        f"{item['name']} ({item.get('fstype')}, {item.get('class')})"
        for item in devices
        if item.get("removable")
        and not item.get("mounted_at")
        and item.get("type") == "part"
    ]
    if idle:
        return _warn(name, f"removable media present but not mounted: {'; '.join(idle)}")
    return _ok(name, "no unmounted removable partitions")


def _check_shortcuts_race(view: _View) -> CheckResult:
    name = "steam_started_after_shortcuts_write"
    race = view.block("shortcuts_race")
    if not race.get("resolved"):
        return _na(name, "shortcuts path not resolved")
    written_after = race.get("written_after_steam_start")
    if written_after is None:
        return _na(name, "Steam not running, or no write time available")
    if written_after:
        return _warn(
            name,
            "shortcuts.vdf was written after Steam started - Steam only "
            "reads it at startup, so missing shortcuts need a restart",
        )
    return _ok(name, f"Steam started {race.get('steam_started')} after the last write")


def _check_third_party_writers(view: _View) -> CheckResult:
    name = "third_party_shortcut_writers"
    installed = view.block("plugins_installed")
    writers = installed.get("known_shortcut_writers") or []
    units = view.block("scheduled_writers").get("candidates") or []
    armed = [item["path"] for item in units if item.get("present")]
    if writers or armed:
        return _warn(
            name,
            f"other shortcut writers present (plugins: {writers or 'none'}; "
            f"units: {armed or 'none'}) - a scanner that rewrites "
            "shortcuts.vdf can drop our entries",
        )
    return _ok(name, "no known third-party shortcut writers detected")


def _check_orphaned_processes(view: _View) -> CheckResult:
    name = "orphaned_toolchain_processes"
    processes = view.block("processes").get("processes") or []
    stuck = [
        f"{item['name']}(pid {item['pid']}, since {item.get('started')})"
        for item in processes
        if item.get("name") in ("wineserver", "upc.exe", "gogdl", "legendary", "nile")
    ]
    if stuck:
        return _warn(
            name,
            f"toolchain processes alive: {'; '.join(stuck)} - if no install "
            "is running these are stranded and can hang the next one",
        )
    return _ok(name, "no stranded toolchain processes")


def _check_session_env(view: _View) -> CheckResult:
    name = "session_env_recoverable"
    block = view.block("session_env")
    if not block.get("steam_running"):
        return _na(name, "Steam not running, nothing to borrow from")
    if not block.get("readable"):
        return _warn(name, "Steam environment unreadable - cannot verify the borrow")
    missing = [key for key, ok in (block.get("variables") or {}).items() if not ok]
    if missing:
        return _fail(
            name,
            f"missing from Steam's environment: {', '.join(missing)} - the "
            "install-time prefix warmup depends on these",
        )
    return _ok(name, "all four session variables recoverable")


def _check_cache_staleness(view: _View) -> CheckResult:
    name = "cache_staleness"
    entries = view.block("caches").get("entries") or []
    if not entries:
        return _na(name, "no cache files")
    stale = [
        f"{Path(item['path']).name} ({item['age_hours']:.0f}h)"
        for item in entries
        if isinstance(item.get("age_hours"), (int, float))
        and item["age_hours"] > _STALE_CACHE_HOURS
    ]
    if stale:
        return _warn(name, f"caches older than 30 days: {'; '.join(stale)}")
    return _ok(name, f"{len(entries)} cache file(s), all fresh")


def _check_clock_and_ca(view: _View) -> CheckResult:
    name = "clock_and_ca_sanity"
    block = view.block("time")
    if not block.get("clock_plausible", True):
        return _fail(
            name,
            f"system clock implausible ({block.get('local_time')}) - TLS will "
            "fail in ways that look like network errors",
        )
    store = block.get("ca_store") or {}
    age = store.get("age_days")
    if isinstance(age, (int, float)) and age > _CA_STORE_MAX_AGE_DAYS:
        return _warn(name, f"CA store last updated {age} days ago ({store.get('path')})")
    return _ok(name, f"clock {block.get('local_time')}, CA store {store.get('entries')} entries")


def _check_prefixes(view: _View) -> CheckResult:
    name = "prefix_count_vs_installed"
    record = view.by_key.get("prefixes")
    if record is None or record.entries is None:
        return _na(name, "no prefixes directory")
    # games.map is a line-oriented text file, not JSON.
    mapped = counts_mod.text_lines(view.by_key.get("games_map"))
    if mapped is None:
        return _ok(name, f"{record.entries} prefix(es); games.map not readable")
    return _ok(name, f"{record.entries} prefix(es), {mapped} games.map entries")


def _check_ntsync(view: _View) -> CheckResult:
    name = "ntsync_available"
    kernel = view.block("kernel")
    if "ntsync_device" not in kernel:
        return _na(name, "kernel block unavailable")
    if kernel.get("ntsync_device"):
        return _ok(name, "/dev/ntsync present")
    return _ok(name, "/dev/ntsync absent - relevant when a Proton hangs at setup")


def _check_vulkan_32bit(view: _View) -> CheckResult:
    name = "vulkan_32bit_present"
    vulkan = view.block("gpu").get("vulkan") or {}
    icds = vulkan.get("icds") or []
    if not icds:
        return _na(name, f"no Vulkan ICD manifests found in {len(vulkan.get('dirs') or [])} dir(s)")
    detail = f"{len(icds)} ICD(s) across {len(vulkan.get('dirs') or [])} dir(s)"
    if vulkan.get("has_32bit") is True:
        return _ok(name, detail)
    if vulkan.get("has_32bit") is False:
        return _warn(
            name,
            f"{detail} - none 32-bit; 32-bit clients (Battle.net) may stall in their "
            "installer without lib32 Mesa/Vulkan",
        )
    return _na(name, f"{detail} - none could be classified")


def _check_decky_log_dir(view: _View) -> CheckResult:
    name = "decky_log_dir_found"
    source = view.ctx.root_sources.get("decky_logs", "unknown")
    if view.ctx.root("decky_logs") is None:
        return _fail(name, "no Decky log directory found - backend logs are absent")
    return _ok(name, f"resolved via {source}")


def _check_config_valid(view: _View) -> CheckResult:
    name = "config_present"
    plugin = view.block("plugin")
    if not plugin.get("resolved"):
        return _na(name, "plugin install directory not resolved")
    if view.present("defaults_config") or plugin.get("flattened_config"):
        return _ok(name, "bundled defaults present")
    return _warn(name, "bundled defaults/config.json not found in the install")


_CHECKS: tuple[Callable[[_View], CheckResult], ...] = (
    _check_not_root,
    _check_decky_log_dir,
    _check_shortcuts_vdf,
    _check_triangulation,
    _check_shortcuts_race,
    check_shortcut_ownership_census,
    check_shortcut_backups,
    _check_third_party_writers,
    _check_steam_root,
    _check_launcher_binary,
    _check_store_binaries,
    _check_umu_runtime,
    _check_ntsync,
    _check_vulkan_32bit,
    _check_session_env,
    _check_orphaned_processes,
    _check_data_dir_writable,
    _check_config_valid,
    _check_auth_consistency,
    _check_storage_visibility,
    _check_unmounted_removable,
    _check_install_fstype,
    _check_disk_space,
    _check_prefixes,
    check_protontricks,
    _check_cache_staleness,
    _check_clock_and_ca,
)


def run_checks(
    ctx: BundleContext, records: list[PathRecord], env: dict[str, Any],
) -> list[CheckResult]:
    """Run every check, isolating failures to their own verdict."""
    view = _View(ctx, records, env)
    results: list[CheckResult] = []
    for check in _CHECKS:
        results.append(_run_one(check, view))
    return results


def _run_one(
    check: Callable[[_View], CheckResult], view: _View,
) -> CheckResult:
    """Run one check, converting an exception into an error verdict."""
    name = getattr(check, "__name__", "check").lstrip("_")
    try:
        return check(view)
    except Exception as err:
        logger.debug("[support_bundle] check %s failed", name, exc_info=True)
        return CheckResult(name=name, status="error", detail=repr(err))


def render_checks(results: list[CheckResult]) -> str:
    """Render the verdict block that leads ``diagnostics.txt``."""
    counts: dict[str, int] = {}
    for item in results:
        counts[item.status] = counts.get(item.status, 0) + 1
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    lines = ["SANITY CHECKS", "-------------", f"  ({summary})", ""]
    for item in results:
        lines.append(f"  [{item.status.upper():<5}] {item.name}")
        if item.detail:
            lines.append(f"          {item.detail}")
    return "\n".join(lines) + "\n"


def failed_count(results: list[CheckResult]) -> int:
    """Number of checks that failed outright (warnings excluded)."""
    return sum(1 for item in results if item.status == "fail")
