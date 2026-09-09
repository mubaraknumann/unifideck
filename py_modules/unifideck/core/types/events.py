"""core/types/events.py — Event names and status enums.

The enums are intentionally kept together in one module because
they're conceptually related (string-typed taxonomies) and none of
them pulls in any runtime dependency — pure value types. A future
split into one-enum-per-file would add import noise without
improving cohesion.

Every enum inherits from `str` so members serialize directly to
JSON without a custom encoder: `json.dumps(Events.SYNC_COMPLETE)`
produces `"sync_complete"`, exactly what the frontend expects.

Reference: Technical Document v1.0 — Section 3.3 (EventBus topology).
"""

from __future__ import annotations

import sys

# UP036/UP042: ruff reads requires-python (3.11) and concludes this block
# is dead and the shim redundant. It is neither. The interpreter that
# imports this module is not always the one the project targets — see
# below — and the shim only ever runs on the interpreter ruff is not
# looking at.
if sys.version_info >= (3, 11):  # noqa: UP036
    from enum import StrEnum
else:
    # StrEnum was only added to the stdlib in Python 3.11. This module sits
    # in the EAGER import chain every Unifideck shortcut launch pulls in
    # (bin/unifideck-launcher -> launcher.dispatcher -> core -> core.types
    # -> .events), which normally runs under a modern system Python via
    # find_python_3_10_plus()'s ACCEPTED_VERSIONS — but that selection only
    # applies to the Proton/umu subprocess, never to the launcher SCRIPT
    # itself. bin/unifideck-launcher's own "#!/usr/bin/env python3" shebang
    # resolves whatever "python3" the CALLING process's environment
    # provides, and when Steam wraps the launcher in its own
    # SteamLinuxRuntime_sniper pressure-vessel container — which it does
    # automatically whenever the user sets Force-Compatibility on a
    # Unifideck shortcut, see
    # launcher.proton.infrastructure.container_escape's docstring for the
    # umu-side twin of this same container problem — that "python3" is the
    # CONTAINER's own (observed: too old for StrEnum, and for the `X | None`
    # syntax core/cache_manager.py used to use unguarded). Reproduced
    # on-device: entering the sniper container directly
    # (_v2-entry-point --verb=run --) and invoking bin/unifideck-launcher
    # failed on exactly this import one level deeper, right after an
    # earlier `Any | None` TypeError in cache_manager.py was fixed.
    #
    # A plain (str, Enum) shim needs no stdlib newer than 3.4, which
    # sidesteps having to know which interpreters are reachable inside an
    # arbitrary Steam Runtime container. It differs from the real StrEnum in
    # exactly one respect — ``str(member)`` would return "Events.NAME"
    # instead of the value — so ``__str__`` is pinned back to ``str``'s.
    # Without that the shim would be a silent behaviour change on the one
    # interpreter nothing here is tested against.
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]  # noqa: UP042
        """Pre-3.11 stand-in for :class:`enum.StrEnum`."""

        __str__ = str.__str__


class Events(StrEnum):
    """All event names emitted on the EventBus.

    ORG: grouped by concern. Adding a new event = one line here +
    a handler subscription somewhere. The `str` base makes the
    name equal to the enum value, which is what subscribers match
    against.

    The frontend mirrors these exact string values in
    `src/SteamBridge.ts` — changing a value here is a breaking
    change for any unreleased frontend build.
    """

    # Plugin lifecycle
    # PLUGIN_LOADED and PLUGIN_UNLOADING lived here and were retired 2026-08.
    # Found by scripts/validate_event_wiring.py while resolving audit §1.3,
    # which did not list them: they were dead in BOTH directions — never
    # emitted, never subscribed — and only looked load-bearing because
    # event_priority.py assigned them EventPriority.CRITICAL. Plugin lifecycle
    # is not event-driven here and never has been: boot runs through
    # bootstrap/boot.py's explicit layer calls and shutdown through
    # services/bootstrap/teardown.py's ordered list, both direct calls with
    # ordering guarantees a bus fan-out could not give them.

    # Sync lifecycle
    SYNC_STARTED = "sync_started"
    SYNC_PROGRESS = "sync_progress"
    SYNC_COMPLETE = "sync_complete"
    SYNC_FAILED = "sync_failed"
    SYNC_CANCELLED = "sync_cancelled"
    # Post-sync enrichment phases — emitted by ArtworkService and
    # MetadataService so the frontend progress bar stays alive
    # through artwork downloads + metadata extraction. Payload:
    #  { phase: "artwork"|"metadata", active: bool, total: int|None, done: int|None }
    POST_SYNC_PHASE_CHANGED = "post_sync_phase_changed"

    # Fired by the fire-and-forget Metacritic backfill
    # (``metadata_backfill``) once its long-tail metacritic.com lookups
    # have all landed in the ``metadata`` cache — AFTER the sync's
    # progress bar already hit 100%. The frontend re-reads library
    # facets on this so newly-backfilled scores surface in Steam's
    # native Sort-by-Metacritic without a manual resync/restart.
    METADATA_BACKFILL_COMPLETE = "metadata_backfill_complete"

    # Durable activity-log events — captured by ActivityLogService
    # into a JSONL file (``runtime_dir/sync_activity.log``) so the
    # frontend can show "last 10 syncs" with timestamps, durations,
    # and per-store counts. Distinct from SYNC_STARTED /
    # SYNC_COMPLETE which are ephemeral UI signals; these carry the
    # data worth persisting.
    #   started   payload: { source, stores, started_at_ms }
    #   completed payload: { source, duration_ms, game_count, errors }
    #   cancelled payload: { source, duration_ms }
    LIBRARY_SYNC_STARTED = "library_sync_started"
    LIBRARY_SYNC_COMPLETED = "library_sync_completed"
    LIBRARY_SYNC_CANCELLED = "library_sync_cancelled"

    # Store auth lifecycle
    STORE_AUTH_STARTED = "store_auth_started"
    STORE_AUTH_COMPLETE = "store_auth_complete"
    STORE_AUTH_FAILED = "store_auth_failed"
    STORE_LOGOUT = "store_logout"

    # Store registration lifecycle — emitted by StoreRegistry when a store
    # plugin is registered at bootstrap. Consumed by
    # src/stores/store-info-store.ts, which refetches store infos on it; the
    # boot-time emit reaches the frontend through the replay buffer on its
    # first poll (the event is in WATCHED_EVENTS and deliberately NOT in
    # STALE_ON_RELOAD_EVENTS, since re-fetching store infos is idempotent).
    # This block claimed metrics_collector.py consumed it until 2026-08; it
    # never has. Audit §1.3 in turn recorded the event as having no consumer
    # at all, which was also wrong — the frontend one is live.
    STORE_REGISTERED = "store_registered"

    # Game lifecycle
    # No GAME_INSTALLED counterpart: install completion is
    # ``DOWNLOAD_COMPLETE`` (worker) → ``mark_installed`` →
    # ``SHORTCUT_INSTALL_STATE_CHANGED``, which fires in BOTH
    # directions and is what flips the ``installed`` flag readers
    # actually observe. A ``GAME_INSTALLED`` member existed for a
    # long time with no live emitter; the asymmetry with
    # GAME_UNINSTALLED made it look real, and three subscribers
    # (artwork, proton, Steam Collections) were silently dead.
    GAME_UNINSTALLED = "game_uninstalled"
    GAME_UPDATE_AVAILABLE = "game_update_available"
    GAME_LAUNCHED = "game_launched"
    GAME_STOPPED = "game_stopped"
    PLAYTIME_UPDATED = "playtime_updated"
    # Playtime → store sync (GOG/Epic) outcome, per drain.
    # Payload: store (str), pushed (int) on complete; store, error on failed.
    # unwired: consumer deferred. Audit §1.3 found _COMPLETE emitted with no
    # subscriber on any leg (its emitter's docstring claimed a toast bridge
    # consumed it — none does) and _FAILED with neither an emitter nor a
    # subscriber. Kept deliberately: surfacing "your playtime reached GOG" is
    # wanted, it just has no UI yet. Wiring it means a frontend subscriber
    # plus rows in src/types/events.ts and WATCHED_EVENTS — all three, or it
    # is silent again.
    PLAYTIME_SYNC_COMPLETE = "playtime_sync_complete"
    # unwired: same deferral as _COMPLETE above, plus this one has no emitter
    # either — PlaytimeSyncService only reports successful drains today.
    PLAYTIME_SYNC_FAILED = "playtime_sync_failed"

    # Power/Sleep lifecycle
    # SUSPEND and RESUME lived here and were retired 2026-08. PlaytimeService
    # was their only subscriber, accumulating a per-session sleep total so a
    # session's duration could be billed as wall-clock minus sleep. Nothing in
    # the tree ever emitted them — no logind/D-Bus listener, no SteamClient
    # hook — so the sleep total was permanently 0 and suspending the Deck
    # mid-game billed the entire suspend as playtime, a number
    # ``PlaytimeSyncService`` then pushed to GOG/Epic. The fix was not to add
    # an emitter: ``PlaytimeService._provisional_duration`` now measures awake
    # time off ``time.monotonic()`` (CLOCK_MONOTONIC does not advance across
    # suspend on Linux), which needs no signal and also covers a hard suspend
    # that would fire none. Do not reintroduce these without a real emitter.

    # Launcher progress stages + the plugin's ONLY user-facing toast
    # channel. Emitted by LauncherService and cloud_failure.py as a
    # game moves through the launch pipeline (prefix setup,
    # cloud sync, proton selection, umu-run start, ...).
    # Also emitted on cloud sync failures, disk space checks,
    # circuit breaker refusals, and shortcut write refusals.
    #
    # Despite the name it is not launch-only: any service that needs to
    # surface an asynchronous message to the user emits here. A separate
    # TOAST_NOTIFICATION member existed until 2026-08 for exactly that
    # "generic notification" job and was retired — it had no subscriber
    # in either process, and its three emitters (circuit breaker,
    # launcher error, shortcut write refusal) were silent for their whole
    # lifetime. Do not reintroduce a second toast channel: this one is
    # the only name wired on both delivery legs.
    #
    # Two legs reach the UI, one per process:
    #   * plugin backend → replay buffer → subscribe_replay →
    #     src/services/boot-event-listener.tsx;
    #   * launcher subprocess → frontend_bridge.install_bus_forwarder →
    #     launcher_events.jsonl → get_launcher_toasts →
    #     src/services/launcherToasts.tsx.
    # The forwarder mirrors THIS event and no other, which is why an
    # event emitted in the launcher on any other name cannot be seen.
    #
    # Payload fields: i18n_key (str), i18n_title_key? (str — bold
    # toast title rendered above i18n_key's message), severity
    # ("info"|"warning"|"error"), i18n_params (dict),
    # duration_ms? (int — overrides the frontend's per-severity
    # default), action? ({i18n_label_key, target_url,
    # fallback_url?}), store?, game_id?, phase?.
    LAUNCHER_STAGE = "launcher_stage"

    # Per-game circuit breaker state transitions. Emitted by
    # LaunchHistoryService._emit_state whenever a failure is recorded, a
    # success wipes the history, failures are cleared, or a bypass is armed.
    #
    # Consumed since 2026-08-26 by ``src/hooks/useCircuitState.ts``, which
    # renders the badge and the two reset controls in PlayMeta (register item
    # 4a). Before that it was polled — it has always been in WATCHED_EVENTS —
    # and dropped, so a user whose game tripped the breaker got a refusal
    # toast, no badge, and no way to reset short of waiting out the window.
    #
    # Payload fields — this is the REAL contract, read off the emit site.
    # Until 2026-08 this block documented `game_key`, `state`, `recent_count`
    # and `failure_kinds`, none of which has ever been sent; CANONICAL_SCHEMA
    # carried the real set, so the gate stayed green while the prose lied. A
    # subscriber written from the old text would read `undefined` for four of
    # five keys and silently render nothing — the same failure as the
    # app_id/game_id split in audit §1.1.1. Verify against the emit site, not
    # against this comment, before adding one:
    #   store (str)          — e.g. "gog"
    #   game_id (str)        — store-scoped id; the emitter splits
    #                          "<store>:<game_id>" itself
    #   is_open (bool)       — whether the breaker is currently refusing
    #   failure_count (int)  — failures inside the window
    #   trigger (str)        — "record_failure_<kind>" (e.g.
    #                          "record_failure_fast_boot"), "clear_failures",
    #                          "arm_bypass", or "closed" after a success
    CIRCUIT_STATE_CHANGED = "circuit_state_changed"

    # Download lifecycle
    DOWNLOAD_QUEUED = "download_queued"
    DOWNLOAD_STARTED = "download_started"
    DOWNLOAD_PROGRESS = "download_progress"
    DOWNLOAD_COMPLETE = "download_complete"
    DOWNLOAD_FAILED = "download_failed"
    DOWNLOAD_CANCELLED = "download_cancelled"

    # Ubisoft install — frontend RunGame trigger. UPC must be opened
    # via Steam's RunGame so it gets its own gamescope/XWayland session
    # in Gaming Mode (a bare backend subprocess has no session to render
    # into → invisible window — the install-never-appears bug). RunGame
    # is a frontend SteamClient API, so the download worker emits this
    # once it has bootstrapped the per-game prefix; the frontend reacts
    # by calling ``launchUbisoftInstallViaShortcut`` with an ``install``
    # action and the worker then monitors the filesystem for the install.
    # Payload fields: store_game_id (str — "ubisoft:<game_id>").
    BATTLENET_INSTALL_LAUNCH_REQUESTED = "battlenet_install_launch_requested"
    UBISOFT_INSTALL_LAUNCH_REQUESTED = "ubisoft_install_launch_requested"

    # STORE_ERROR lived here and was retired 2026-08 (audit §1.3). It was a
    # generic store-failure toast channel that NEVER had a backend emitter —
    # ``git log -S store_error -- py_modules/`` reaches back to the initial
    # commit and finds none. It was not a regression; the emitting half was
    # never built, while the frontend half (a handler in boot-event-listener
    # rendering ``toasts.storeError``, translated in all 16 locales) sat there
    # looking wired. Every failure it could have carried is already covered:
    # a per-store sync failure logs, emits SYNC_FAILED and toasts via
    # LAUNCHER_STAGE (core/sync_run_mixin.py); an auth failure logs, emits
    # STORE_AUTH_FAILED and flips the store's status in the frontend
    # (stores/shared/store_registry.py).

    # Frontend CEF capability probes, reported back for ProbeReactionService
    # and FeatureFlagService to react to. Payload: probes (dict).
    # unwired: emitter deferred to remediation register 4i. There is no probe
    # suite in src/, so the event has never fired, so both subscribers have
    # never run and feature flags have never left their defaults. 4i decides
    # the pipeline as one unit — build the suite and the reporting route, or
    # delete both services and their maps. Do not wire only this half.
    RUNTIME_PROBES_REPORTED = "runtime_probes_reported"

    # security audit events. Emitted by the
    # security package + token managers + auth flows. Consumed
    # by SecurityService for audit logging, counters, and
    # centralised policy enforcement.
    SECURITY_TOKEN_ENCRYPTED = "security_token_encrypted"  # noqa: S105 — event name constant, not a credential
    SECURITY_TOKEN_DECRYPTED = "security_token_decrypted"  # noqa: S105 — event name constant, not a credential
    SECURITY_DECRYPT_FAILED = "security_decrypt_failed"
    SECURITY_TOKEN_FILE_MIGRATED = "security_token_file_migrated"  # noqa: S105 — event name constant, not a credential
    SECURITY_LEGACY_PLAINTEXT_DETECTED = "security_legacy_plaintext_detected"
    SECURITY_AUTH_FLOW_STARTED = "security_auth_flow_started"
    SECURITY_AUTH_FLOW_COMPLETED = "security_auth_flow_completed"
    SECURITY_AUTH_FLOW_FAILED = "security_auth_flow_failed"
    # token age policy. Emitted by token managers when a load
    # finds a payload whose `_unifideck_encrypted_at` metadata is
    # older than the manager's configured ``max_token_age``. The
    # token file is treated as unusable (forced re-auth) and the
    # event is surfaced to the audit log + counters so operators
    # can correlate "user kicked out" with the policy decision
    # rather than guessing it was a server-side revocation.
    SECURITY_TOKEN_AGE_EXCEEDED = "security_token_age_exceeded"  # noqa: S105 — event name constant, not a credential

    # active policy events. Emitted either by
    # token managers (permissions check at each save) or by
    # SecurityService itself when a policy triggers an action.
    SECURITY_PERMISSIONS_CHECK = "security_permissions_check"
    SECURITY_PERMISSIONS_REPAIRED = "security_permissions_repaired"
    SECURITY_BRUTEFORCE_SUSPECTED = "security_bruteforce_suspected"
    SECURITY_DEVICE_RESET_DETECTED = "security_device_reset_detected"
    SECURITY_FINGERPRINT_INITIALIZED = "security_fingerprint_initialized"

    # observability for stores whose credentials are
    # managed by external CLIs (legendary/nile) or Wine prefixes
    # (Ubisoft Connect). Unifideck does not own these tokens but
    # it does read their status at every sync, and anomalies in
    # those reads are worth tracking for diagnostics. Emitted
    # only on REAL anomalies (missing CLI binary, corrupt file,
    # missing prefix assets) — NOT on the routine "user isn't
    # logged in yet" case, which would pollute the audit log.
    SECURITY_EXTERNAL_AUTH_CHECK_FAILED = "security_external_auth_check_failed"

    # config validation at boot. Emitted by
    # ConfigValidator.validate_config after schema validation
    # completes, regardless of outcome. Handlers live in
    # SecurityService (or future ConfigService) and record the
    # result in the audit log for operator diagnostics. The
    # _COMPLETED variant carries defaults_validated + user_overrides_present
    # flags; _FAILED additionally carries error_count + first_error_source
    # + first_error_path so operators can jump to the broken section
    # without parsing the full errors list.
    CONFIG_VALIDATION_COMPLETED = "config_validation_completed"
    CONFIG_VALIDATION_FAILED = "config_validation_failed"

    # SUBSCRIPTION_DETECTED / _EXPIRED / _CHECK_FAILED lived here and were
    # retired 2026-08 (audit §1.3). MicrosoftSubscriptionService emitted them
    # on every tier transition and this docstring claimed "Subscribers are the
    # frontend toast listener and MetricsCollector" — neither existed. They
    # were missing THREE legs, not one: no Python subscriber, absent from
    # src/types/events.ts, and absent from WATCHED_EVENTS, so the frontend
    # never even polled for them. SYNC_SKIPPED below carries the same news on
    # a channel that is actually rendered; do not add a second one.

    # Generic "store chose not to sync" event.
    # Distinct from SYNC_FAILED (which implies an error): SYNC_SKIPPED
    # is an intentional no-op with a user-facing explanation. Today
    # emitted only by MicrosoftStore when the Game Pass subscription
    # check returns NONE, ACTIVE_UNKNOWN, or an error. Future
    # subscription-based stores (EA Play, Ubisoft+) would emit the
    # same event with their own reason string.
    #
    # Consumed by src/services/boot-event-listener.tsx, which maps ``reason``
    # to an explanatory toast through its SYNC_SKIPPED_KEYS table and ignores
    # reasons it does not know. Adding a reason here means adding a row there,
    # or the skip is silent again — which is exactly how a failed Game Pass
    # check used to drop the whole xCloud library with no message while the
    # sync bar reported success for the other five stores.
    # Payload fields: store (str), reason (str)
    SYNC_SKIPPED = "sync_skipped"

    # Steam account switch detection. Emitted by AccountService when
    # the user signs into a different Steam account (detected by
    # polling loginusers.vdf for a MostRecent user id change).
    # Every store-scoped cache subscribes and invalidates entries for
    # the previous account so library/subscription/token state does
    # not leak across Steam profiles.
    # Payload fields:
    #   active_user_id (str)  — the new MostRecent id
    #   new_user (str)        — the same value, kept for backward compat;
    #                           UserPathsCoordinator accepts either key
    # There is no `previous_user_id`: this block documented one until 2026-08
    # and AccountService has never sent it (audit §1.3).
    ACCOUNT_SWITCHED = "account_switched"

    # ShortcutService lifecycle. Emitted whenever a shortcut is added
    # or removed from shortcuts.vdf so interested services
    # (ArtworkService, MetricsCollector) can react without polling.
    # Payload fields for SHORTCUT_CREATED:
    #   store (str), app_id (int, signed), unsigned_id (int, u32),
    #   title (str), is_auth (bool)
    SHORTCUT_CREATED = "shortcut_created"

    # Emitted by ShortcutService when an entry is removed from
    # games.map (and consequently from shortcuts.vdf on the next
    # save). Mirrors SHORTCUT_CREATED so interested services
    # (ArtworkService, MetricsCollector) can react without polling.
    # Added 2026-05-15 (lot 12c): the emit site in
    # services/shortcut/games_map_mixin.py:233 has always referenced
    # ``Events.SHORTCUT_REMOVED`` but the enum member was never
    # declared — the call was a silent no-op (mypy attr-defined).
    # Payload fields: app_id (int, signed).
    SHORTCUT_REMOVED = "shortcut_removed"

    # Emitted by ShortcutService when an existing shortcut's
    # install state flips (post-install or post-uninstall) without
    # the shortcut itself being created or removed. The shortcut
    # appid stays anchored on (launcher_path, "<store>:<store_game_id>")
    # across the transition — see SyncService._backfill_app_ids —
    # so this event is the canonical channel for "the game at app_id
    # N just became (un)installed". SyncService updates _all_games
    # and the frontend refreshes its unifideckGameCache entry.
    # Payload fields:
    #   store (str), store_game_id (str), app_id (int, signed),
    #   installed (bool), exe_path (str, "" on uninstall),
    #   install_path (str, "" on uninstall).
    SHORTCUT_INSTALL_STATE_CHANGED = "shortcut_install_state_changed"

    # Emitted by ShortcutService once a bulk reconcile (post-sync)
    # finishes. Carries the per-batch counters so the frontend can
    # decide whether to prompt the user for a Steam restart (any
    # ``added`` > 0 or ``removed`` > 0 invalidates Steam's in-memory
    # copy of shortcuts.vdf — without a restart, Steam overwrites
    # our changes on its next shutdown). Payload fields:
    #   added (int), removed (int), kept (int), total (int)
    SHORTCUT_RECONCILE_COMPLETE = "shortcut_reconcile_complete"

    # ── UI toast notification ────────────────────────────────────
    # TOAST_NOTIFICATION lived here and was retired 2026-08. Declaring
    # the member in 2026-05 fixed only the AttributeError its three emit
    # sites were raising — it never made them reach the UI, because the
    # name was absent from src/types/events.ts and WATCHED_EVENTS, had no
    # Python subscriber, and was not mirrored by the launcher's bus
    # forwarder (which carries LAUNCHER_STAGE alone). All three emitters
    # now use LAUNCHER_STAGE; see its comment above. Generic toasts have
    # one channel on purpose.

    # On-demand artwork fetch request. Any caller may emit this to
    # ask ArtworkService to pull covers for a given title from
    # SteamGridDB. ArtworkService deduplicates by app_id (won't
    # fetch if artwork already present unless force=True).
    # Payload fields: app_id (int), title (str), store (str, opt),
    #   game_id (str, opt), force (bool, opt, default False)
    # unwired: no emitter yet. Audit §1.3 confirmed there has never been one.
    # The handler is retained on purpose as the entry point for a force-refetch
    # trigger (the `force=True` arm exists for the account-switch case, where
    # existing art is stale) — it just needs a caller. The two artwork paths
    # that DO run reach ArtworkService by other routes: the bulk post-sync
    # phase (POST_SYNC_PHASE_CHANGED) and auth tiles (SHORTCUT_CREATED).
    ARTWORK_REQUEST = "artwork_request"
    # ── Cloud-save sync lifecycle ────────────────────────────────
    # Emitted by ``CloudSaveService`` to surface per-game save
    # transfer outcomes to the UI. The DOWN events fire on the
    # game→local pull (pre-launch); the UP events fire on the
    # local→cloud push (post-exit). ``COMPLETE`` carries
    # ``synced: bool`` so the UI can distinguish "ran the sync
    # but had no changes" from "skipped entirely"; ``FAILED``
    # carries an ``error`` string for the toast text.
    # Common payload fields: store (str), game_id (str).
    # COMPLETE adds: synced (bool).
    # FAILED adds: error (str).
    CLOUD_SYNC_DOWN_COMPLETE = "cloud_sync_down_complete"
    CLOUD_SYNC_DOWN_FAILED = "cloud_sync_down_failed"
    CLOUD_SYNC_UP_COMPLETE = "cloud_sync_up_complete"
    CLOUD_SYNC_UP_FAILED = "cloud_sync_up_failed"


class StoreStatus(StrEnum):
    """Store availability after a status check."""

    UNAVAILABLE = "unavailable"
    NOT_AUTHENTICATED = "not_authenticated"
    AVAILABLE = "available"
    ERROR = "error"


class StoreEnum(StrEnum):
    """Canonical store IDs used as dict keys and frontend routes."""

    EPIC = "epic"
    GOG = "gog"
    AMAZON = "amazon"
    MICROSOFT = "microsoft"
    UBISOFT = "ubisoft"
    BATTLENET = "battlenet"


class OwnershipType(StrEnum):
    """How a game is owned (full purchase vs subscription)."""

    OWNED = "owned"
    SUBSCRIBED = "subscribed"
    TRIAL = "trial"
    UNKNOWN = "unknown"


class GameTag(StrEnum):
    """Filters applied by the UI to group/hide games."""

    NATIVE = "native"
    PROTON = "proton"
    CLOUD = "cloud"
    XCLOUD = "xcloud"
    DLC = "dlc"
    BETA = "beta"
    DEMO = "demo"
    HIDDEN = "hidden"


class ErrorCode(StrEnum):
    """Normalized error codes across stores.

    Store connectors convert their raw errors (HTTP status,
    subprocess exit code, API string) into one of these values so
    the frontend can match on stable identifiers instead of
    parsing free-form messages.
    """

    NOT_AUTHENTICATED = "not_authenticated"
    TOKEN_EXPIRED = "token_expired"  # noqa: S105 — event name constant, not a credential
    NETWORK_ERROR = "network_error"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    QUOTA_EXCEEDED = "quota_exceeded"
    INSUFFICIENT_SPACE = "insufficient_space"
    BINARY_MISSING = "binary_missing"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class SubscriptionTier(StrEnum):
    """Subscription tier for stores whose catalog depends on a paid plan.

    Sprint 18e scope is Microsoft / Xbox Game Pass. The three paid
    tiers (Essential, Premium, Ultimate) are listed for forward
    compatibility — the current implementation can only discriminate
    NONE vs. any-active until real probe responses from each tier
    are captured and parsed (Sprint 18f).

    ACTIVE_UNKNOWN is the conservative bucket for "the probe responded
    200 OK but couldn't parse a tier marker". Callers treat it as
    "skip the sync" (Sprint 18e Q1 decision) to avoid showing users
    games they can't actually stream.

    The enum inherits from str so members serialize directly to JSON
    for EventBus payloads: json.dumps(SubscriptionTier.ULTIMATE)
    produces "ultimate", which is what the frontend expects.
    """

    NONE = "none"
    ESSENTIAL = "essential"
    PREMIUM = "premium"
    ULTIMATE = "ultimate"
    ACTIVE_UNKNOWN = "active_unknown"
