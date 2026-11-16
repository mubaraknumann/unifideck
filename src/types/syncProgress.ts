/**
 * Library sync progress payload.
 *
 * Emitted by the backend SyncService and consumed by the
 * sync progress bar in `<QuickAccessPanel>`. The shape is
 * append-only — fields are added when new sync stages land,
 * but never removed mid-version.
 */
export interface SyncProgressCurrentGame {
  label: string;
  values: Record<string, string | number>;
}

/** Where one store's row is in the current run. */
export type StoreSyncState =
  | "queued"
  | "active"
  | "waiting"
  | "done"
  | "error"
  | "cancelled";

/** The step a store's row is on. `""` for an artwork-only run's start. */
export type StoreSyncPhase = "games" | "metadata" | "artwork" | "compat" | "";

/**
 * One store's progress in the current run (`SyncProgress.stores` in
 * `core/sync_progress.py`). `done` / `total` count that store's games
 * in the current phase.
 */
export interface StoreSyncRow {
  state: StoreSyncState;
  phase: StoreSyncPhase;
  done: number;
  total: number;
  error: string | null;
}

/** A store's library as of its last sync, for the idle row. */
export interface StoreSyncSummary {
  count: number;
  /** Unix seconds; `null` when the store has never synced. */
  synced_at: number | null;
}

/** The request waiting behind the running one. `stores: null` = all. */
export interface QueuedSync {
  kind: "sync" | "force" | "artwork";
  stores: string[] | null;
}

/**
 * Live snapshot of the active sync : current store, total
 * vs done count, optional ETA. Polled by the SyncContext
 * provider while a sync is running.
 */
export interface SyncProgress {
  total_games: number;
  synced_games: number;
  current_game: SyncProgressCurrentGame;
  status: string;
  progress_percent: number;
  error?: string;
  // Artwork tracking
  artwork_total?: number;
  artwork_synced?: number;
  // Per-source metadata tracking (incremented in lockstep by
  // MetadataService._run_enrichment — three sources run in
  // parallel via asyncio.gather; one row per source on the UI).
  steam_total?: number;
  steam_synced?: number;
  unifidb_total?: number;
  unifidb_synced?: number;
  metacritic_total?: number;
  metacritic_synced?: number;
  // Compatibility phase (proton_meta) — ProtonDB tier +
  // Deck-Verified status per game. Backend tracks this in
  // SyncProgress.compat_total / compat_synced.
  compat_total?: number;
  compat_synced?: number;
  // Per-store rows for the stores the current run covers.
  stores?: Record<string, StoreSyncRow>;
  // Per-store library summary, reported whether or not a sync runs.
  store_summary?: Record<string, StoreSyncSummary>;
  queued?: QueuedSync | null;
  // Lifecycle flags
  restart_pending?: boolean;
  is_cancelling?: boolean;
  request_source?: string;
  run_id?: number;
  started_at?: number;
  finished_at?: number;
}
