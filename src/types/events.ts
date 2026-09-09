/**
 * EventBus event names — mirror of backend `core/types/events.py`.
 *
 * Names and lowercase string values match the backend Events
 * enum exactly. The frontend subscribes via `subscribe_replay`
 * (see `api/event-bus-client.ts`) ; these are the wire strings
 * the backend emits, so a typo here breaks the bridge silently.
 *
 * Single source of truth on the frontend. Components never
 * reference event strings directly — they import `Events`.
 */
export const Events = {
  // Authentication (backend prefixes auth events with STORE_)
  STORE_AUTH_STARTED: "store_auth_started",
  STORE_AUTH_COMPLETE: "store_auth_complete",
  STORE_AUTH_FAILED: "store_auth_failed",
  STORE_LOGOUT: "store_logout",
  STORE_REGISTERED: "store_registered",
  // Library sync
  SYNC_STARTED: "sync_started",
  SYNC_PROGRESS: "sync_progress",
  SYNC_COMPLETE: "sync_complete",
  SYNC_FAILED: "sync_failed",
  SYNC_CANCELLED: "sync_cancelled",
  SYNC_SKIPPED: "sync_skipped",
  // Durable activity-log events for the "recent syncs" panel.
  // Persisted to ``runtime_dir/sync_activity.log`` by
  // ActivityLogService; the frontend can also subscribe directly
  // if it wants live updates.
  LIBRARY_SYNC_STARTED: "library_sync_started",
  LIBRARY_SYNC_COMPLETED: "library_sync_completed",
  LIBRARY_SYNC_CANCELLED: "library_sync_cancelled",
  // Post-sync phase signal. Emitted by ArtworkService and
  // MetadataService when their background work finishes (active=false).
  // Payload: {phase: "artwork"|"metadata", active: bool, total, done}.
  // The frontend clears `isSyncing` when both phases report done.
  POST_SYNC_PHASE_CHANGED: "post_sync_phase_changed",
  // Fired by the background Metacritic backfill once its long-tail
  // metacritic.com lookups have landed (after the progress bar hit
  // 100%). The overview-enrichment layer re-reads facets on this so
  // newly-backfilled scores surface in Steam's native Sort-by-Metacritic
  // without a manual resync/restart.
  METADATA_BACKFILL_COMPLETE: "metadata_backfill_complete",
  // Emitted by ShortcutService once reconcile finishes; payload
  // carries {added, removed, kept, total} so the UI can decide
  // whether to prompt for a Steam restart.
  SHORTCUT_RECONCILE_COMPLETE: "shortcut_reconcile_complete",
  // Emitted by ShortcutService when an existing shortcut's install
  // state flips (post-install/uninstall, no shortcut recreation).
  // Payload: { store, store_game_id, app_id, installed,
  //            exe_path, install_path }. Drives unifideckGameCache
  //  refresh so the GOG tab and detail-page UI react immediately.
  SHORTCUT_INSTALL_STATE_CHANGED: "shortcut_install_state_changed",
  // Downloads
  DOWNLOAD_QUEUED: "download_queued",
  DOWNLOAD_STARTED: "download_started",
  DOWNLOAD_PROGRESS: "download_progress",
  DOWNLOAD_COMPLETE: "download_complete",
  DOWNLOAD_FAILED: "download_failed",
  DOWNLOAD_CANCELLED: "download_cancelled",
  // Ubisoft install — backend asks the frontend to open Ubisoft Connect
  // via RunGame (so UPC gets a gamescope session in Gaming Mode). Emitted
  // by the download worker once the per-game prefix is bootstrapped.
  // Payload: { store_game_id: "ubisoft:<game_id>" }.
  UBISOFT_INSTALL_LAUNCH_REQUESTED: "ubisoft_install_launch_requested",
  BATTLENET_INSTALL_LAUNCH_REQUESTED: "battlenet_install_launch_requested",
  // Game state
  GAME_UNINSTALLED: "game_uninstalled",
  GAME_UPDATE_AVAILABLE: "game_update_available",
  GAME_LAUNCHED: "game_launched",
  GAME_STOPPED: "game_stopped",
  // Cloud-save sync (CloudSaveService)
  CLOUD_SYNC_DOWN_COMPLETE: "cloud_sync_down_complete",
  CLOUD_SYNC_DOWN_FAILED: "cloud_sync_down_failed",
  CLOUD_SYNC_UP_COMPLETE: "cloud_sync_up_complete",
  CLOUD_SYNC_UP_FAILED: "cloud_sync_up_failed",
  // Errors and toasts
  LAUNCHER_STAGE: "launcher_stage",
  CIRCUIT_STATE_CHANGED: "circuit_state_changed",
} as const;

/**
 * Union of every event name the EventBus may emit. Derived
 * from the `Events` constant so adding a new event in one
 * place updates this type in lockstep.
 */
export type EventName = (typeof Events)[keyof typeof Events];

/**
 * Standard payload fields for events that carry a toast or
 * a follow-up action (e.g. LAUNCHER_STAGE on cloud sync
 * failure). The backend includes `action` when there is a
 * URI verb the frontend should expose as a button. Frontend
 * dispatches that URI via `dispatch_unifideck_action`.
 */
export interface ToastActionPayload {
  severity?: "info" | "warning" | "error";
  i18n_key?: string;
  /** Optional bold title rendered above `i18n_key`'s message. */
  i18n_title_key?: string;
  i18n_params?: Record<string, unknown>;
  /** Resolved game title. Merged into the i18n params as `gameTitle`. */
  game_title?: string;
  duration_ms?: number;
  /**
   * One-click remediation offered with the toast.
   *
   * `i18n_label_key` names it. Two producers emit this: the cloud-save
   * conflict path (which also sends `local_snapshot`/`remote_snapshot`, and
   * is rendered as a pick modal) and `cloud_failure` (transient failures,
   * rendered as a clickable toast). Both use this one shape — a second
   * `{i18n_label_key, target_url}` form existed until 2026-08-26 and would
   * have read `undefined` here (audit register item 4b).
   */
  action?: { verb: string; args: string[]; i18n_label_key?: string };
}
