/**
 * sync-store — boot-time singleton for library sync state.
 *
 * Replaces the QAM-bound EventBus subscriptions and 500ms polling
 * loop that previously lived inside `<SyncProvider>`. The store:
 *
 *   1. Subscribes to SYNC_* events at boot so state transitions
 *      are never missed (even with QAM closed mid-sync).
 *   2. Runs the adaptive 500ms `get_sync_progress` poll while a
 *      sync is in flight.
 *   3. Manages the deferred Steam-restart modal (staged on
 *      SHORTCUT_RECONCILE_COMPLETE, shown after all post-sync
 *      phases complete).
 *   4. Dispatches `unifideck-sync-completed` custom events for
 *      CollectionManager and LibraryContext.
 *
 * React components subscribe via `useSyncExternalStore` through
 * the thin `<SyncProvider>` wrapper.
 */
import { call } from "@decky/api";
import { showModal } from "@decky/ui";
import { rpcRoutes } from "../api/rpc-routes";
import { unwrapRpcEnvelope } from "../api/useRPC";
import { EventBusClient } from "../api/event-bus-client";
import { setSyncCooldownMs } from "../hooks/useSyncCooldown";
import type { SyncProgress } from "../types/syncProgress";
import { SteamRestartModal } from "../components/modals/SteamRestartModal";

const PROGRESS_POLL_MS = 500;

/** Sentinel for an event that carries no usable `run_id`. */
const UNTAGGED_RUN_ID = -1;

/**
 * Read a generation id off an event payload.
 *
 * Mirrors `run_id_of` in `core/sync_generation.py`. Returns
 * `UNTAGGED_RUN_ID` when the key is absent or not a real number, so a
 * replayed pre-migration event degrades to "always current" rather than
 * being silently dropped.
 */
function readRunId(payload: unknown): number {
  const raw = (payload as Record<string, unknown>)?.run_id;
  return typeof raw === "number" && Number.isInteger(raw)
    ? raw
    : UNTAGGED_RUN_ID;
}

// ── Snapshot type ────────────────────────────────────────

export interface SyncSnapshot {
  progress: SyncProgress | null;
  isSyncing: boolean;
  isCancelling: boolean;
}

// ── Store implementation ────────────────────────────────

type Listener = () => void;

class SyncStoreImpl {
  private _snapshot: SyncSnapshot = {
    progress: null,
    isSyncing: false,
    isCancelling: false,
  };
  private _listeners = new Set<Listener>();
  private _unsubs: (() => void)[] = [];
  private _pollTimer: ReturnType<typeof setInterval> | null = null;
  private _pendingPhases = new Set<string>();
  private _observedActiveSync = false;
  private _pendingRestart = false;
  // Generation of the sync we are currently draining phases for, latched
  // from `sync_started`. `_pendingPhases` is a single mutable set that
  // `sync_started` replaces wholesale, so without a run id a phase-done
  // event from a superseded sync drained THIS sync's set — which flipped
  // isSyncing to false and popped the Steam-restart modal while the live
  // sync was still downloading artwork. Measured 2026-08-29: three artwork
  // batches alive at once, one announcing done for a 645-game generation
  // against a library that had been 1242 games for two minutes.
  // `null` = no tagged sync seen yet (fail open, see `_isStaleRun`).
  private _currentRunId: number | null = null;

  /** Start subscriptions and initial state restore. */
  start(): void {
    // Restore state — a sync may already be in flight from
    // before the frontend loaded.
    void this._pollOnce();

    this._unsubs.push(
      EventBusClient.subscribe("sync_started", (payload) => {
        this._observedActiveSync = true;
        // Seed the phase set from the backend's authoritative
        // ``registered_phases`` rather than a hardcoded list. The old
        // hardcoded {artwork,metadata,proton_meta} over-counted when the
        // real set was smaller (no CompatibilityService → no proton_meta),
        // so the set never drained and the Steam-restart modal never fired
        // (UD-006). Fall back to the legacy set for replay/older backends.
        // An artwork skip is drained by its own phase-done event.
        const phases = (payload as { registered_phases?: string[] })
          ?.registered_phases;
        this._pendingPhases = new Set(
          Array.isArray(phases) && phases.length
            ? phases
            : ["artwork", "metadata", "proton_meta"],
        );
        // Latch this run's generation; later phase events must match it.
        this._currentRunId = readRunId(payload);
        // A new generation supersedes any restart armed by the previous
        // one — that reconcile's counters described a library state this
        // run is about to replace.
        this._pendingRestart = false;
        this._update({ isSyncing: true, isCancelling: false });
        EventBusClient.bumpToFast();
        this._startPolling();
      }),
    );

    this._unsubs.push(
      EventBusClient.subscribe("sync_progress", (payload) => {
        this._update({ progress: payload as unknown as SyncProgress });
      }),
    );

    this._unsubs.push(
      EventBusClient.subscribe("sync_complete", () => {
        this._update({ isCancelling: false });
        // Do NOT set isSyncing=false here — wait for post-sync phases.
        window.dispatchEvent(new CustomEvent("unifideck-sync-completed"));
      }),
    );

    this._unsubs.push(
      EventBusClient.subscribe("post_sync_phase_changed", (payload) => {
        const phase = String((payload as Record<string, unknown>)?.phase ?? "");
        const active = Boolean(
          (payload as Record<string, unknown>)?.active ?? false,
        );
        if (active || !phase) return;
        // Ignore a phase-done belonging to a superseded generation.
        if (this._isStaleRun(payload)) return;
        this._pendingPhases.delete(phase);
        if (this._pendingPhases.size === 0) {
          this._update({ isSyncing: false, isCancelling: false });
          this._stopPolling();
          if (this._pendingRestart && this._observedActiveSync) {
            this._pendingRestart = false;
            try {
              showModal(
                <SteamRestartModal reason="sync" closeModal={() => {}} />,
              );
            } catch (e) {
              console.error(
                "[SyncStore] showModal(SteamRestartModal) failed",
                e,
              );
            }
          } else if (this._pendingRestart) {
            // Replay path: clear flag so a later sync can re-arm.
            this._pendingRestart = false;
          }
        }
      }),
    );

    this._unsubs.push(
      EventBusClient.subscribe("sync_failed", (payload) => {
        // `sync_failed` is emitted PER STORE — its only emitter is
        // `_sync_one_store`'s exception path, and its payload is
        // {store, error}. It does not mean the run ended: the loop
        // carries on with the remaining stores and finalizes normally,
        // reporting the failure through SYNC_COMPLETE's `errors`.
        //
        // Treating it as terminal (clearing `_pendingPhases`, flipping
        // isSyncing off) meant one flaky store left the set empty, so the
        // very next phase-done saw size === 0 and fired the Steam-restart
        // modal after metadata — before artwork and compat had run. Leave
        // the phase set alone; the drain and the 1800s backend watchdog
        // both still terminate the run.
        console.warn(
          "[SyncStore] store failed during sync:",
          (payload as Record<string, unknown>)?.store,
          (payload as Record<string, unknown>)?.error,
        );
      }),
    );

    this._unsubs.push(
      EventBusClient.subscribe("sync_cancelled", () => {
        this._pendingPhases.clear();
        this._update({
          isSyncing: false,
          isCancelling: false,
          progress: null,
        });
        this._stopPolling();
      }),
    );

    this._unsubs.push(
      EventBusClient.subscribe("shortcut_reconcile_complete", (payload) => {
        if (this._isStaleRun(payload)) return;
        const added = Number(payload?.added ?? 0);
        const removed = Number(payload?.removed ?? 0);
        // Deliberately NOT armed on `reclaimed`, even though the payload
        // now carries it. `_try_reclaim_orphan` is tried ahead of the
        // LaunchOptions match, so every already-registered game counts as
        // reclaimed on every sync (997 of them on a sync that added and
        // removed nothing) — arming on it would show the restart modal
        // after literally every sync. Only added/removed change what
        // Steam needs to re-read.
        if (added > 0 || removed > 0) {
          this._pendingRestart = true;
        }
      }),
    );
  }

  /** Stop all subscriptions and polling. */
  stop(): void {
    this._stopPolling();
    for (const unsub of this._unsubs) unsub();
    this._unsubs = [];
  }

  /** Notify the store that a sync was started by the user
   *  (called from SyncContext before the RPC). */
  notifySyncStarted(): void {
    this._observedActiveSync = true;
    this._update({
      isSyncing: true,
      isCancelling: false,
      progress: null,
    });
    EventBusClient.bumpToFast();
    this._startPolling();
    void this._pollOnce();
  }

  /** Notify the store that a cancel was requested. */
  notifyCancelRequested(): void {
    this._update({ isCancelling: true, progress: null });
  }

  /**
   * Whether an event belongs to a run older than the one being drained.
   *
   * Fails open in both untagged directions — an event with no `run_id`,
   * or a store that has not yet latched one from `sync_started` — so a
   * partially-migrated backend keeps the pre-run-id behaviour instead of
   * stranding `_pendingPhases` forever (which would hang the progress bar
   * and mean the restart modal never fires again).
   */
  private _isStaleRun(payload: unknown): boolean {
    const incoming = readRunId(payload);
    if (incoming === UNTAGGED_RUN_ID || this._currentRunId === null) {
      return false;
    }
    return incoming !== this._currentRunId;
  }

  // ── useSyncExternalStore API ──────────────────────────

  getSnapshot = (): SyncSnapshot => this._snapshot;

  subscribe = (listener: Listener): (() => void) => {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  };

  // ── Internals ─────────────────────────────────────────

  private async _pollOnce(): Promise<void> {
    try {
      const raw = await call<[], unknown>(rpcRoutes.getSyncProgress);
      const data = unwrapRpcEnvelope<
        SyncProgress & { syncing?: boolean; cooldown_ms?: number }
      >(raw, { route: rpcRoutes.getSyncProgress, throwing: false });
      if (!data) return;

      const partial: Partial<SyncSnapshot> = { progress: data };
      if (typeof data.syncing === "boolean") {
        partial.isSyncing = data.syncing;
        if (data.syncing) {
          // A sync is in flight (e.g. restored at boot, or a
          // background/scheduled run). Keep the 500ms loop alive so
          // progress refreshes — we no longer rely on a replayed
          // ``sync_started`` to start polling (those are primed past
          // on reload). ``get_status`` reports syncing through the
          // post-sync phases too, so this also self-clears the bar
          // (status → "complete") when the run actually finishes.
          this._startPolling();
        } else {
          partial.isCancelling = false;
          this._stopPolling();
        }
      }
      if (typeof data.cooldown_ms === "number" && data.cooldown_ms >= 0) {
        setSyncCooldownMs(data.cooldown_ms);
      }
      this._update(partial);
    } catch (e) {
      console.warn("[SyncStore] poll failed", e);
    }
  }

  private _startPolling(): void {
    if (this._pollTimer) return;
    this._pollTimer = setInterval(
      () => void this._pollOnce(),
      PROGRESS_POLL_MS,
    );
  }

  private _stopPolling(): void {
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  private _update(partial: Partial<SyncSnapshot>): void {
    this._snapshot = { ...this._snapshot, ...partial };
    this._emit();
  }

  private _emit(): void {
    for (const listener of this._listeners) listener();
  }
}

/** Singleton — started at boot from `definePlugin`. */
export const syncStore = new SyncStoreImpl();
