"""support_bundle/sources_audit.py — Audited but never collected.

Rows whose *existence* is diagnostic but whose contents must not — or
need not — leave the device.

Three kinds:

``presence_only``
    Credentials and identity files. We stat them and report size,
    mtime and mode; we never open them. "Your GOG token is missing"
    explains a login failure in one line, and today discovering that
    costs a full round-trip with the reporter.

``bulk``
    Directories measured and counted, never walked into the archive:
    Wine prefixes (tens of GB and full of user registry data), the
    browser profile, save data, the umu runtime payload.

``static``
    Reference catalogs we ship ourselves. Byte-identical for every
    user, so including them would be a megabyte of noise.

Plus the infrastructure rows — the launcher binary and the bundled
store CLIs — where "present and executable" is the whole question.
"""
from __future__ import annotations

from .sources import COLLECTED
from .spec import SourceSpec

# ── Credentials: stat only, never read ────────────────────────────
_SECRETS: tuple[SourceSpec, ...] = (
    SourceSpec(
        key="epic_auth_url", what="Epic OAuth sign-in URL (carries a code verifier)",
        root="data", pattern="epic_auth_url.txt", policy="presence_only",
        expect="optional", writer="stores/epic/auth.py",
    ),
    SourceSpec(
        key="gog_auth_url", what="GOG OAuth sign-in URL",
        root="data", pattern="gog_auth_url.txt", policy="presence_only",
        expect="optional", writer="stores/gog/config.py",
    ),
    SourceSpec(
        key="ms_auth_url", what="Microsoft OAuth sign-in URL",
        root="data", pattern="ms_auth_url.txt", policy="presence_only",
        expect="optional", writer="stores/microsoft/microsoft_browser_auth.py",
    ),
    SourceSpec(
        key="amazon_auth_url", what="Amazon OAuth sign-in URL",
        root="data", pattern="amazon_auth_url.txt", policy="presence_only",
        expect="optional", writer="stores/amazon/amazon_auth.py",
    ),
    SourceSpec(
        key="gog_token", what="GOG access/refresh token",
        root="config", pattern="gog_token.json", policy="presence_only",
        expect="gog", writer="accounts/account_manager.py",
        note="Absent while the UI claims GOG is connected is a real bug.",
    ),
    SourceSpec(
        key="gogdl_auth", what="gogdl's own credential store",
        root="config", pattern="gogdl_auth.json", policy="presence_only",
        expect="gog", writer="accounts/account_manager.py",
    ),
    SourceSpec(
        key="microsoft_token", what="Microsoft/Xbox token",
        root="config", pattern="microsoft_token.json", policy="presence_only",
        expect="microsoft", writer="accounts/account_manager.py",
    ),
    SourceSpec(
        key="legendary_user", what="legendary's Epic account credentials",
        root="home", pattern=".config/legendary/user.json",
        policy="presence_only", expect="epic", writer="legendary",
    ),
    SourceSpec(
        key="nile_user", what="nile's Amazon account credentials",
        root="home", pattern=".config/nile/user.json",
        policy="presence_only", expect="amazon", writer="nile",
    ),
    SourceSpec(
        key="butler_db", what="butler's itch.io profile and API key database",
        root="data", pattern="butler/butler.db",
        policy="presence_only", expect="itch", writer="butler",
        note="Holds the itch.io API key: statted, never opened.",
    ),
    SourceSpec(
        key="ubisoft_upc_session", what="Ubisoft Connect session token",
        root="data", pattern="ubisoft_upc_session.txt",
        policy="presence_only", expect="ubisoft", writer="stores/ubisoft",
    ),
    SourceSpec(
        key="device_fingerprint", what="Per-device identity blob",
        root="config", pattern="device_fingerprint.json",
        policy="presence_only", expect="optional", writer="security",
    ),
    SourceSpec(
        key="playtime_wal", what="Playtime DB write-ahead log",
        root="data", pattern="playtime.db-wal", policy="presence_only",
        expect="launch", writer="sqlite3",
        note="Excluded for size; its presence explains a stale .db snapshot.",
    ),
)

# ── Bulk directories: measured, never walked in ───────────────────
_BULK: tuple[SourceSpec, ...] = (
    SourceSpec(
        key="prefixes", what="Per-game Wine prefixes",
        root="data", pattern="prefixes", policy="bulk", expect="launch",
        writer="launcher/proton/infrastructure/core.py",
        note="Tens of GB, and they contain the user's Wine registry.",
    ),
    SourceSpec(
        key="ubisoft_prefixes",
        what="Ubisoft prefix namespace (template, auth, per-game)",
        root="data", pattern="prefixes/ubisoft", policy="bulk",
        expect="ubisoft", writer="stores/ubisoft/config.py",
        note="Nested one level deeper than every other store, so the "
             "top-level prefixes row counts this whole namespace as a "
             "single entry. Audited separately to make the template / "
             "auth / per-game lifecycle visible.",
    ),
    SourceSpec(
        key="saves", what="Local save data staged for cloud sync",
        root="data", pattern="saves", policy="bulk", expect="optional",
        writer="services/cloud_save",
    ),
    SourceSpec(
        key="save_backups", what="Pre-sync save backups",
        root="data", pattern="save_backups", policy="bulk", expect="optional",
        writer="services/cloud_save/safety.py",
    ),
    SourceSpec(
        key="edge_auth_profile", what="Chromium profile used for OAuth",
        root="data", pattern="edge-auth", policy="bulk", expect="optional",
        writer="auth/edge_browser/edge.py",
        note="Contains cookies and saved credentials.",
    ),
    SourceSpec(
        key="ubisoft_installer_cache", what="Cached Ubisoft Connect installer",
        root="data", pattern="ubisoft_installer_cache", policy="bulk",
        expect="ubisoft", writer="stores/ubisoft/installer",
    ),
    SourceSpec(
        key="compat_tools", what="Compat tools staged by the plugin",
        root="data", pattern="compat-tools", policy="bulk", expect="optional",
        writer="launcher/proton/infrastructure/selector.py",
    ),
    SourceSpec(
        key="grid_dir", what="Steam artwork directory for our shortcuts",
        root="paths", pattern="grid_dir", policy="bulk", expect="sync",
        writer="services/artwork",
    ),
    SourceSpec(
        key="umu_cache", what="umu runtime payload (pressure-vessel trees)",
        root="home", pattern=".local/share/umu", policy="bulk",
        expect="launch", writer="umu-run",
        note="Hundreds of MB. Completeness is checked, contents excluded.",
    ),
)

# ── Shipped reference data ────────────────────────────────────────
_STATIC: tuple[SourceSpec, ...] = (
    SourceSpec(
        key="ubisoft_uuid_catalog", what="Ubisoft UUID catalog from unifiDB",
        root="data", pattern="ubisoft_uuid_catalog.json", policy="static",
        expect="ubisoft", writer="stores/ubisoft catalog refresh",
    ),
    SourceSpec(
        key="ubisoft_game_db", what="Ubisoft game database dump",
        root="data", pattern="ubisoft_game_db.txt", policy="static",
        expect="ubisoft", writer="stores/ubisoft catalog refresh",
    ),
)

# ── Steam files with account data: audited, not shipped ───────────
_STEAM_PRIVATE: tuple[SourceSpec, ...] = (
    SourceSpec(
        key="config_vdf", what="Steam config.vdf (holds CompatToolMapping)",
        root="steam", pattern="config/config.vdf", policy="skip",
        expect="always", writer="Steam",
        note="Contains account data. The environment report extracts only "
             "the CompatToolMapping entries for our AppIDs plus the "
             "appid-0 global default.",
    ),
    SourceSpec(
        key="localconfig_vdf", what="Steam per-user localconfig.vdf",
        root="paths", pattern="config_vdf_path", policy="skip",
        expect="always", writer="Steam",
        note="Contains account data and every app's launch options.",
    ),
    SourceSpec(
        key="loginusers_vdf", what="Steam loginusers.vdf (account list)",
        root="paths", pattern="loginusers_path", policy="skip",
        expect="always", writer="Steam",
    ),
)

# ── Infrastructure: is it there and is it executable ─────────────
_BINARIES: tuple[SourceSpec, ...] = (
    SourceSpec(
        key="launcher_bin", what="Shortcut launcher entry point",
        root="paths", pattern="launcher_path", policy="presence_only",
        expect="always", writer="shipped in bin/",
        note="Every shortcut Steam launches points at this file.",
    ),
    SourceSpec(
        key="legendary_bin", what="Bundled legendary CLI (Epic)",
        root="plugin", pattern="bin/legendary", policy="presence_only",
        expect="always", writer="shipped in bin/",
    ),
    SourceSpec(
        key="gogdl_bin", what="Bundled gogdl CLI (GOG)",
        root="plugin", pattern="bin/gogdl", policy="presence_only",
        expect="always", writer="shipped in bin/",
    ),
    SourceSpec(
        key="nile_bin", what="Bundled nile CLI (Amazon)",
        root="plugin", pattern="bin/nile", policy="presence_only",
        expect="always", writer="shipped in bin/",
    ),
    SourceSpec(
        key="comet_bin", what="Bundled comet (GOG Galaxy features)",
        root="plugin", pattern="bin/comet", policy="presence_only",
        expect="always", writer="shipped in bin/",
    ),
    SourceSpec(
        key="butler_bin", what="Bundled butler CLI (itch.io)",
        root="plugin", pattern="bin/butler/butler", policy="presence_only",
        expect="always", writer="shipped in bin/butler/",
    ),
    SourceSpec(
        key="butler_7z_lib", what="butler's bundled 7-zip library",
        root="plugin", pattern="bin/butler/7z.so", policy="presence_only",
        expect="always", writer="shipped in bin/butler/",
        note="Missing means butler downloads it at runtime on the first 7z "
             "install, which fails offline.",
    ),
    SourceSpec(
        key="umu_run_bin", what="Bundled umu-run launcher shim",
        root="plugin", pattern="bin/umu-run", policy="presence_only",
        expect="optional", writer="shipped in bin/",
    ),
    # Two valid layouts, so neither path alone is "always" expected:
    # the source tree ships defaults/config.json while the Decky CLI
    # build flattens it to config.json at the install root. Marking
    # either as required reported a phantom missing file on every real
    # install; the ``config_present`` check is what verifies that one of
    # the two actually exists.
    SourceSpec(
        key="defaults_config", what="Bundled default configuration (source layout)",
        root="plugin", pattern="defaults/config.json", policy="presence_only",
        expect="optional", writer="shipped in defaults/",
        note="Absent on installs built by the Decky CLI, which flattens "
             "this to config.json at the install root.",
    ),
    SourceSpec(
        key="flattened_config", what="Bundled default configuration (packaged layout)",
        root="plugin", pattern="config.json", policy="presence_only",
        expect="optional", writer="Decky CLI build",
        note="The flattened twin of defaults/config.json. Exactly one of "
             "the two should be present.",
    ),
)

AUDIT_ONLY: tuple[SourceSpec, ...] = (
    *_SECRETS, *_BULK, *_STATIC, *_STEAM_PRIVATE, *_BINARIES,
)


def all_sources() -> tuple[SourceSpec, ...]:
    """Return the complete registry, collected rows first.

    Collected rows lead so the collector can walk this in one pass in
    priority order without re-filtering, and so the audit table reads
    top-down from "what we shipped" to "what we only looked at".
    """
    return (*COLLECTED, *AUDIT_ONLY)
