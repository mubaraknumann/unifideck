"""support_bundle/sources.py — Collected artifact rows.

The half of the registry that contributes bytes to the archive. The
presence-only, bulk and static rows live in :mod:`sources_audit`;
:func:`all_sources` returns both.

**This table is an allowlist.** No row globs the data or config dir
wholesale, so a future change that drops a new secret into
``~/.local/share/unifideck/`` cannot leak it without also adding a
row here — a diff a reviewer will see. That property is enforced by
a test, not just by convention.

``priority`` is also the budget order: when the total-uncompressed cap
is reached the remaining rows are skipped, so the lowest numbers are
the artifacts we least want to lose.
"""
from __future__ import annotations

from .spec import (
    CAP_DECKY_SESSION,
    CAP_EDGE_LOG,
    CAP_GAME_LOG,
    CAP_JSON_LARGE,
    CAP_SMALL_JSON,
    CAP_STEAM_LOG,
    CAP_VDF,
    MAX_DECKY_FILES,
    MAX_GAME_LOGS,
    MAX_LAUNCH_LOGS,
    MAX_PROTON_LOGS,
    SourceSpec,
)

# ── Logs (priority 10-40) ─────────────────────────────────────────
_LOGS: tuple[SourceSpec, ...] = (
    SourceSpec(
        key="decky_session_logs",
        what="Decky Loader plugin log — one file per plugin_loader session",
        root="decky_logs", pattern="*.log", arch_dir="decky",
        policy="tail", max_bytes=CAP_DECKY_SESSION, scrub="text",
        priority=10, newest_n=MAX_DECKY_FILES, expect="always",
        writer="Decky Loader captures the plugin process stdout/stderr",
        note="Filenames contain spaces; sanitised in the archive.",
    ),
    SourceSpec(
        key="launch_python_logs",
        what="Per-launch backend log written by the out-of-process launcher",
        root="launches", pattern="*.log", exclude="*.game.log",
        arch_dir="launches", policy="include", max_bytes=CAP_GAME_LOG,
        scrub="text", priority=20, newest_n=MAX_LAUNCH_LOGS, expect="launch",
        writer="launcher/diagnostics/log_archive.py::attach_launch_handler",
    ),
    SourceSpec(
        key="launch_game_logs",
        what="Per-launch umu / Proton / game stdout+stderr",
        root="launches", pattern="*.game.log", arch_dir="launches",
        policy="tail", max_bytes=CAP_GAME_LOG, scrub="text",
        priority=30, newest_n=MAX_GAME_LOGS, expect="launch",
        writer="launcher/proton/infrastructure/game_log.py::open_game_log",
        note="Install-time prefix warmup runs outside a launch id, so a "
             "'-.game.log' orphan is normal and often the largest file.",
    ),
    SourceSpec(
        key="vendor_client_logs",
        what="A wrapper store's own client logs, salvaged before its prefix was deleted",
        root="launches", pattern="*.vendor.txt", arch_dir="launches",
        policy="tail", max_bytes=CAP_GAME_LOG, scrub="text",
        priority=25, newest_n=MAX_LAUNCH_LOGS, expect="optional",
        writer="stores/shared/prefix_forensics.py::preserve_vendor_logs",
        note="For a wrapper store the prefix IS the install, so a failed "
             "install deletes the only first-hand account of why the vendor "
             "client would not start. This is that account, kept.",
    ),
    SourceSpec(
        key="launcher_events",
        what="Launcher-to-frontend toast bridge (JSONL, capped at 100 lines)",
        root="data", pattern="launcher_events.jsonl", arch_dir="data",
        policy="tail", scrub="jsonl", priority=40, expect="launch",
        writer="launcher/frontend_bridge.py::record_event",
    ),
    SourceSpec(
        key="sync_activity",
        what="Rotating library-sync activity log (JSONL despite the .log name)",
        root="data", pattern="sync_activity.log", arch_dir="data",
        policy="tail", scrub="jsonl", priority=40, expect="sync",
        writer="services/activity_log.py::ActivityLogService",
    ),
    # Steam's own logs. Curated rather than wholesale: the logs
    # directory is ~186 MB on a real device, nearly all of it CEF and
    # webhelper noise. These are the ones that carry launch, compat and
    # install failures. The rest are enumerated in inventory.txt.
    SourceSpec(
        key="steam_compat_log",
        what="Steam's compatibility-tool log (which Proton it chose, and why)",
        root="steam", pattern="logs/compat_log.txt", arch_dir="steam-logs",
        policy="tail", max_bytes=CAP_STEAM_LOG, scrub="text",
        priority=22, expect="always", writer="Steam client",
        note="Where a wrong-Proton or failed-compat-tool launch shows up.",
    ),
    SourceSpec(
        key="steam_console_log",
        what="Steam client console output",
        root="steam", pattern="logs/console-linux.txt", arch_dir="steam-logs",
        policy="tail", max_bytes=CAP_STEAM_LOG, scrub="text",
        priority=24, expect="always", writer="Steam client",
    ),
    SourceSpec(
        key="steam_content_log",
        what="Steam download / install activity",
        root="steam", pattern="logs/content_log.txt", arch_dir="steam-logs",
        policy="tail", max_bytes=CAP_STEAM_LOG, scrub="text",
        priority=26, expect="always", writer="Steam client",
    ),
    SourceSpec(
        key="steam_gameprocess_log",
        what="Steam game process start / exit records",
        root="steam", pattern="logs/gameprocess_log.txt", arch_dir="steam-logs",
        policy="tail", max_bytes=CAP_STEAM_LOG, scrub="text",
        priority=26, expect="always", writer="Steam client",
        note="Shows whether Steam ever started our shortcut, and its exit code.",
    ),
    SourceSpec(
        key="steam_cloud_log",
        what="Steam Cloud sync activity",
        root="steam", pattern="logs/cloud_log.txt", arch_dir="steam-logs",
        policy="tail", max_bytes=CAP_STEAM_LOG, scrub="text",
        priority=28, expect="optional", writer="Steam client",
    ),
    SourceSpec(
        key="steam_shader_log",
        what="Shader pre-caching activity",
        root="steam", pattern="logs/shader_log.txt", arch_dir="steam-logs",
        policy="include", max_bytes=CAP_STEAM_LOG, scrub="text",
        priority=28, expect="optional", writer="Steam client",
    ),
    SourceSpec(
        key="proton_debug_logs",
        what="PROTON_LOG output (~/steam-<appid>.log)",
        root="home", pattern="steam-*.log", arch_dir="proton",
        policy="tail", max_bytes=CAP_GAME_LOG, scrub="text",
        priority=24, newest_n=MAX_PROTON_LOGS, expect="optional",
        writer="Proton, when the user sets PROTON_LOG=1",
        note="The canonical Proton debug log. Only present when someone "
             "turned it on, which usually means they were already "
             "debugging the exact failure being reported.",
    ),
    SourceSpec(
        key="edge_auth_log",
        what="Chromium/Edge stderr from OAuth sign-in windows",
        root="data", pattern="edge-auth.log", arch_dir="data",
        policy="tail", max_bytes=CAP_EDGE_LOG, scrub="text_aggressive",
        priority=99, expect="optional",
        writer="auth/edge_browser/edge.py",
        note="Aggressively scrubbed and line-filtered: it is stderr from a "
             "live OAuth browser, so a navigation error can print a "
             "redirect URL containing an authorization code.",
    ),
)

# ── Small state files (priority 50) ───────────────────────────────
_STATE_SMALL: tuple[SourceSpec, ...] = (
    SourceSpec(
        key="settings", what="Plugin UI settings (locale, toggles)",
        root="data", pattern="settings.json", arch_dir="data",
        scrub="json", priority=50, expect="always",
        writer="accounts/account_manager.py::SETTINGS_PATH",
    ),
    SourceSpec(
        key="user_config", what="User config overrides layered over the defaults",
        root="config", pattern="config.json", arch_dir="config",
        scrub="json", priority=50, expect="optional",
        writer="config/user_config_path.py::resolve_user_config_path",
    ),
    SourceSpec(
        key="proton_settings", what="Per-game Proton/compat-tool overrides",
        root="data", pattern="proton_settings.json", arch_dir="data",
        scrub="json", priority=50, expect="optional",
        writer="compatibility/proton_helpers.py",
    ),
    SourceSpec(
        key="proton_ge_latest", what="Marker for the auto-installed latest GE build",
        root="data", pattern="proton_ge_latest.json", arch_dir="data",
        scrub="json", priority=50, expect="optional",
        writer="launcher/proton/infrastructure/ge_installer.py",
    ),
    SourceSpec(
        key="download_queue", what="Install/update queue as last persisted",
        root="data", pattern="download_queue.json", arch_dir="data",
        scrub="json", priority=50, expect="always",
        writer="services/download/persistence.py",
    ),
    SourceSpec(
        key="game_sizes", what="Cached on-disk size per installed game",
        root="data", pattern="game_sizes.json", arch_dir="data",
        scrub="json", priority=50, expect="optional",
        writer="services/size_cache.py",
    ),
    SourceSpec(
        key="cloud_sync_state", what="Per-game cloud-save sync bookkeeping",
        root="data", pattern="cloud_sync_state.json", arch_dir="data",
        scrub="json", priority=50, expect="optional",
        writer="services/cloud_save/gog_state_mixin.py",
    ),
    SourceSpec(
        key="lastplaytime_marker", what="One-shot playtime-reset marker",
        root="data", pattern="lastplaytime_reset.done", arch_dir="data",
        scrub="none", priority=50, expect="optional",
        writer="services/playtime",
    ),
)

# ── Medium state files (priority 55-60) ───────────────────────────
_STATE_MEDIUM: tuple[SourceSpec, ...] = (
    SourceSpec(
        key="download_history", what="Completed/failed install history",
        root="data", pattern="download_history.json", arch_dir="data",
        scrub="json", priority=55, expect="optional",
        writer="services/download/service.py",
    ),
    SourceSpec(
        key="launch_history", what="Per-game launch failures (circuit breaker)",
        root="data", pattern="launch_history.json", arch_dir="data",
        scrub="json", priority=55, expect="launch",
        writer="services/launch_history/persistence.py",
    ),
    SourceSpec(
        key="achievement_state", what="Last-session achievement snapshot",
        root="data", pattern="achievement_state.json", arch_dir="data",
        scrub="json", priority=55, expect="optional",
        writer="services/achievements/state.py",
    ),
    SourceSpec(
        key="ubisoft_id_map", what="Ubisoft game id to per-game prefix mapping",
        root="data", pattern="ubisoft_id_map.json", arch_dir="data",
        max_bytes=CAP_SMALL_JSON, scrub="json", priority=55, expect="ubisoft",
        writer="stores/ubisoft/config.py",
    ),
    SourceSpec(
        key="ubisoft_visible_games", what="Ubisoft titles surfaced after dedup",
        root="data", pattern="ubisoft_visible_games.json", arch_dir="data",
        max_bytes=CAP_SMALL_JSON, scrub="json", priority=55, expect="ubisoft",
        writer="stores/ubisoft/config.py",
    ),
    SourceSpec(
        key="battlenet_id_map",
        what="Battle.net uid to family code, prefix and proven-launch state",
        root="data", pattern="battlenet_id_map.json", arch_dir="data",
        max_bytes=CAP_SMALL_JSON, scrub="json", priority=55, expect="battlenet",
        writer="stores/battlenet/id_map.py",
    ),
    SourceSpec(
        key="steam_owned_titles", what="Titles already owned on Steam (dedup input)",
        root="data", pattern="steam_owned_titles.json", arch_dir="data",
        max_bytes=CAP_SMALL_JSON, scrub="json", priority=55, expect="sync",
        writer="steam/owned_games.py",
    ),
    SourceSpec(
        key="steam_owned_games",
        what="Titles + appids already owned on Steam (cross-store grouping input)",
        root="data", pattern="steam_owned_games.json", arch_dir="data",
        max_bytes=CAP_SMALL_JSON, scrub="json", priority=55, expect="sync",
        writer="steam/owned_games.py",
    ),
    SourceSpec(
        key="games_map", what="app_id to store/game mapping used by the launcher",
        root="paths", pattern="games_map_path", arch_dir="data",
        scrub="text", priority=60, expect="sync",
        writer="services/shortcut",
    ),
)

# ── Plugin install metadata (priority 70) ─────────────────────────
_PLUGIN_META: tuple[SourceSpec, ...] = (
    SourceSpec(
        key="package_json", what="Installed plugin version (source of truth)",
        root="plugin", pattern="package.json", arch_dir="plugin",
        scrub="none", priority=70, expect="always",
    ),
    SourceSpec(
        key="plugin_json", what="Decky plugin manifest (name, flags)",
        root="plugin", pattern="plugin.json", arch_dir="plugin",
        scrub="none", priority=70, expect="always",
    ),
    SourceSpec(
        key="dev_build", what="Dev-build marker written by build-plugin.sh",
        root="plugin", pattern="dev_build.json", arch_dir="plugin",
        scrub="none", priority=70, expect="optional",
        note="Present means this is a dev deploy, not a release install.",
    ),
)

# ── Large but high-value (priority 80-92) ─────────────────────────
_HEAVY: tuple[SourceSpec, ...] = (
    SourceSpec(
        key="shortcuts_registry",
        what="Our record of every shortcut we created (app_id to game)",
        root="data", pattern="shortcuts_registry.json", arch_dir="data",
        max_bytes=CAP_JSON_LARGE, scrub="json", priority=80, expect="sync",
        writer="services/shortcut/registry.py",
        note="Compared against shortcuts.vdf and games.map by the "
             "shortcut_count_triangulation check.",
    ),
    SourceSpec(
        key="shortcuts_vdf",
        what="Steam's non-Steam shortcut database (binary VDF)",
        root="paths", pattern="shortcuts_path", arch_dir="steam",
        max_bytes=CAP_VDF, scrub="none", priority=85, expect="sync",
        writer="Steam, plus services/shortcut on sync",
        note="Mode matters: losing the exec bit made an external tool "
             "wipe library entries.",
    ),
    SourceSpec(
        key="shortcuts_vdf_backups",
        what="Pre-write snapshots of shortcuts.vdf (newest first)",
        root="data", pattern="shortcuts_backups/shortcuts.vdf.bak.*",
        arch_dir="steam",
        max_bytes=CAP_VDF, scrub="none", priority=85, expect="sync",
        writer="services/shortcut/vdf_backup.py before every write",
        note="Diff against shortcuts_vdf to see exactly which entries a "
             "write removed - the question every 'my non-Steam games "
             "disappeared' report turns on.",
    ),
    SourceSpec(
        key="libraryfolders_vdf",
        what="Steam's library folder list (which drives Steam knows about)",
        root="steam", pattern="steamapps/libraryfolders.vdf", arch_dir="steam",
        max_bytes=CAP_SMALL_JSON, scrub="none", priority=85, expect="always",
        writer="Steam",
        note="'My SD library is not detected' is often this list, not ours.",
    ),
    SourceSpec(
        key="playtime_db",
        what="Playtime database (SQLite)",
        root="paths", pattern="playtime_db", arch_dir="data",
        max_bytes=CAP_JSON_LARGE, scrub="none", priority=88, expect="launch",
        writer="services/playtime/db.py",
        note="The -wal sidecar is excluded, so this snapshot can lag. The "
             "generated playtime.summary.json reads the live DB read-only "
             "and is the accurate row count.",
    ),
    SourceSpec(
        key="library_cache",
        what="Cached merged library across all connected stores",
        root="data", pattern="library_cache.json", arch_dir="data",
        max_bytes=CAP_JSON_LARGE, scrub="json", priority=90, expect="sync",
        writer="launcher/dispatcher.py",
        note="Primary artifact for 'game missing from my library' reports.",
    ),
    SourceSpec(
        key="legendary_config", what="legendary (Epic) CLI configuration",
        root="home", pattern=".config/legendary/config.ini", arch_dir="config",
        max_bytes=CAP_SMALL_JSON, scrub="text", priority=92, expect="epic",
        writer="legendary",
    ),
    SourceSpec(
        key="legendary_installed", what="legendary's installed-game manifest",
        root="home", pattern=".config/legendary/installed.json",
        arch_dir="config", max_bytes=CAP_JSON_LARGE, scrub="json",
        priority=92, expect="epic", writer="legendary",
    ),
    SourceSpec(
        key="nile_config", what="nile (Amazon) CLI configuration",
        root="home", pattern=".config/nile/config.json", arch_dir="config",
        max_bytes=CAP_SMALL_JSON, scrub="json", priority=92, expect="amazon",
        writer="nile",
    ),
    SourceSpec(
        key="nile_installed", what="nile's installed-game manifest",
        root="home", pattern=".config/nile/installed.json", arch_dir="config",
        max_bytes=CAP_JSON_LARGE, scrub="json", priority=92, expect="amazon",
        writer="nile",
    ),
)

COLLECTED: tuple[SourceSpec, ...] = (
    *_LOGS, *_STATE_SMALL, *_STATE_MEDIUM, *_PLUGIN_META, *_HEAVY,
)
