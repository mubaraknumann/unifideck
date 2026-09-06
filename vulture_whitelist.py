"""Vulture whitelist — names that are live but have no static caller.

Rewritten 2026-08-26 (audit register item 27). Read the two findings below
before adding anything.

**1. At the gate's own threshold this file suppresses nothing.** CI runs
``vulture py_modules/unifideck vulture_whitelist.py`` with
``min_confidence = 80`` (``pyproject.toml``). Measured: **0 hits with this
file and 0 without it.** At 60 it suppresses 29 of 308. So every entry here
is inert against the check that actually runs — which is why the file rotted
unnoticed, and it is the same blind spot as register item 24: at 80, vulture
reports neither unused functions nor whole unimported modules.

**2. A group comment lies about individual members.** The old file grouped
entries under a shared justification. ``StoreInfo.uses_wine`` sat under
"StoreInfo fields are read through attribute access by every store" for a
release with no reader anywhere (audit §3.1) — in the one file whose job is
recording why something *looks* dead but is not. It was not alone: of 51
entries, **22 suppressed nothing even at confidence 60**, including six names
that do not exist in the tree at all (``Plugin._uninstall``,
``Plugin._migration``, ``tb``, ``exc_tb``, ``connect_to_steam``,
``is_steam_ui_tab``) and four with ordinary static callers
(``resolve_user_config_path`` — 8 call sites, ``clear_store_cookies`` — 5,
``get_bool`` — 3, and ``_config_degraded``, live since the §1.2 pass). All 22
are deleted.

**How every entry below was verified, and how to verify a new one.** Not by
reading: delete the line, re-run ``vulture py_modules/unifideck
vulture_whitelist.py --min-confidence 60``, and check whether a new hit
appears. If none does, the entry silences nothing and does not belong here.
A name that suppresses no report is not documentation — it is a claim nobody
checks, which is precisely how ``uses_wine`` survived.

Each entry carries its **own** reason, on the line above it. Do not
reintroduce group comments.
"""

# ── Bootstrap entry points ───────────────────────────────────────

# Called by main.Plugin._main, the Decky load hook.
boot_plugin  # noqa: B018, F821

# Called by main.Plugin._unload.
unload_plugin  # noqa: B018, F821

# ── Back-compat alias ────────────────────────────────────────────

# Alias in config/i18n_schema.py kept for importers of the old name; the
# canonical spelling is validate_i18n_schema.
validate_i18n  # noqa: B018, F821

# ── Reached over the RPC bridge, not from Python ─────────────────

# compatibility/proton_helpers.py — called from the Proton picker's RPC mixin.
list_known_tools  # noqa: B018, F821

# actions/unifideck_uri.py. NOT a proven-live entry: it has no frontend
# caller either. A deletion candidate, kept only so the report stays quiet
# while the dead-verb decision (register item 4b) is open.
list_supported_verbs  # noqa: B018, F821

# ── core/io/async_file_ops — callers import the module, not the name ──

# Used by services/artwork/fetcher.py through the module object.
ensure_dir  # noqa: B018, F821

# Same module, same access pattern.
read_json  # noqa: B018, F821

# Same module, same access pattern.
write_json  # noqa: B018, F821

# ── CacheManager admin surface ───────────────────────────────────

# core/cache_manager.py — definition-only today; reached from the wipe path.
clear_all  # noqa: B018, F821

# core/cache_manager.py — diagnostic read.
cache_size  # noqa: B018, F821

# core/cache_manager.py — diagnostic read.
registered_names  # noqa: B018, F821

# ── CDP / Edge browser, driven by the auth orchestrator ──────────

# cdp/cdp_client.py — called through the injected client object.
navigate  # noqa: B018, F821

# cdp/cdp_client.py — same.
wait_for_url  # noqa: B018, F821

# auth/edge_browser/edge.py — invoked by the storefront launch flow.
prepare_auth_launch  # noqa: B018, F821

# auth/edge_browser/edge.py — teardown counterpart.
close_auth_browser  # noqa: B018, F821

# cdp/page_inject.py — consumed by a decorator, never referenced by name.
_session_timeout  # noqa: B018, F821

# ── Attribute assigned once, read reflectively ───────────────────

# ProtonToolManager instance attribute (compatibility/proton_helpers.py).
_shortcuts_registry_path  # noqa: B018, F821

# ── StoreInfo fields, read by attribute off the payload dict ─────
# Reasons are per-name on purpose: the group form of this exact comment is
# what let ``uses_wine`` sit here unread for a release (audit §3.1).
# Capability flags are NOT here any more — they are derived in
# ``get_store_infos`` from ``core/store_capabilities.py``, so there is no
# per-store field left to whitelist.

# Sent in the get_store_infos payload.
auth_method  # noqa: B018, F821

# Sent in the get_store_infos payload.
icon_asset  # noqa: B018, F821

# Sent in the get_store_infos payload. Descriptive only — audit §3.5 found
# the install refusal is enforced inside the store, not from this flag.
supports_install  # noqa: B018, F821

# ── Typed containers constructed dynamically ─────────────────────

# Protocol in event_bus/event_bus_extensions.py.
EventPayload  # noqa: B018, F821

# metadata/unifidb.py.
UnifiDBResult  # noqa: B018, F821

# services/playtime/db.py.
PlaySessionResult  # noqa: B018, F821

# services/playtime/db.py.
GameStatsResult  # noqa: B018, F821

# services/playtime/db.py; has a TS twin in src/types/playtime.ts.
DailyTotal  # noqa: B018, F821

# ── Enum members looked up by value, not by attribute ────────────
# Emitters pass the string name (services/security/permissions.py,
# device_reset.py, service.py), so no static ``Events.X`` reference exists.

SECURITY_PERMISSIONS_REPAIRED  # noqa: B018, F821
SECURITY_BRUTEFORCE_SUSPECTED  # noqa: B018, F821
SECURITY_DEVICE_RESET_DETECTED  # noqa: B018, F821

# ── Validator, kept under protest ────────────────────────────────

# core/types/identifiers.py. NOT a proven-live entry: zero callers found.
# Listed only because removing it produces a report. Deletion candidate.
is_safe_game_id  # noqa: B018, F821
