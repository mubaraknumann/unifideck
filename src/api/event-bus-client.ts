/**
 * Bidirectional bridge to the backend EventBus.
 *
 * Backend emits events on its in-process Python EventBus.
 * Decky doesn't expose a server-push channel, so we use
 * `subscribe_replay(events)` which returns a snapshot of the
 * recent buffered events with their full kwargs payload. We
 * deduplicate by timestamp on the frontend side ; any event
 * we've already dispatched is filtered out.
 *
 * Reverse direction : `dispatch_unifideck_action(uri)` lets
 * the frontend trigger a backend action (auth, retry-sync,
 * refresh-library, ...). The URI form is
 * `unifideck://<verb>[/arg1/arg2]`. Toast/modal payloads
 * coming through events embed an optional `action` block
 * which the UI exposes as a button — clicking it dispatches
 * the corresponding URI back to the backend.
 *
 * Polling cadence is adaptive : 250ms when an operation is
 * in flight (events arriving), 2s when idle.
 */
import { call } from "@decky/api";
import { useEffect, useRef } from "react";
import { rpcRoutes } from "./rpc-routes";
import { unwrapRpcEnvelope } from "./useRPC";
import type { EventName } from "../types/events";

const POLL_FAST_MS = 250;
const POLL_SLOW_MS = 2000;

/** All events the frontend ever cares about. The backend
 *  buffers each event type up to its capacity (50 for
 *  PROGRESS, 20 for state changes — see backend EventReplay
 *  defaults). Asking for an event type the backend doesn't
 *  buffer is safe : we get an empty list. */
const WATCHED_EVENTS: EventName[] = [
  "store_auth_started",
  "store_auth_complete",
  "store_auth_failed",
  "store_logout",
  "store_registered",
  "sync_started",
  "sync_progress",
  "sync_complete",
  "sync_failed",
  "sync_cancelled",
  "sync_skipped",
  "post_sync_phase_changed",
  "metadata_backfill_complete",
  "shortcut_reconcile_complete",
  "shortcut_install_state_changed",
  "download_queued",
  "download_started",
  "download_progress",
  "download_complete",
  "download_failed",
  "download_cancelled",
  // The backend emits this after bootstrapping a Ubisoft per-game prefix to
  // ask the frontend to RunGame the UPC shortcut. It MUST be polled here or
  // the download-store handler never fires and the install hangs forever on
  // "Installing Ubisoft Connect" (the rest of the chain — RunGame → launcher
  // → UPC — works; this allowlist omission was the whole bug).
  "ubisoft_install_launch_requested",
  "battlenet_install_launch_requested",
  "game_uninstalled",
  "game_update_available",
  "game_launched",
  "game_stopped",
  "cloud_sync_down_complete",
  "cloud_sync_down_failed",
  "cloud_sync_up_complete",
  "cloud_sync_up_failed",
  "launcher_stage",
  "circuit_state_changed",
];

/** Imperative events that *do something* when dispatched (here: RunGame →
 *  open UPC) rather than just updating idempotent UI state. They must NOT be
 *  re-fired from the backend's replay backlog on a fresh load — otherwise a
 *  Steam restart relaunches UPC once per buffered event. They're primed past
 *  (watermark advanced, not dispatched) on the first poll after load; events
 *  emitted live during the session still fire normally. */
const IMPERATIVE_EVENTS = new Set<string>([
  "ubisoft_install_launch_requested",
  "battlenet_install_launch_requested",
]);

/** Sync-lifecycle events describe a sync that was already underway or
 *  finished in a PRIOR session. ``SteamRestartModal`` only restarts the
 *  Steam *client*; the Decky backend (and its in-memory replay buffer)
 *  keeps running, so on the next load these would replay from timestamp 0
 *  on the first poll — resurrecting the progress bar (stale ``sync_progress``)
 *  and re-showing the restart modal (``sync_started`` re-arms
 *  ``_observedActiveSync`` so the replayed ``shortcut_reconcile_complete``
 *  fires the prompt again). The authoritative restore is
 *  ``syncStore.start()`` → ``get_sync_progress``, so prime past these on the
 *  first poll after load; events emitted live during the session still fire
 *  normally (their timestamps exceed the watermark). */
const STALE_ON_RELOAD_EVENTS = new Set<string>([
  // A failure from a PRIOR session, replayed from timestamp 0, re-applies a
  // store status of "error" that the authoritative restore (`authStore.start()`
  // → `check_store_status`) had just answered correctly. That status is sticky
  // for the rest of the session, so a single old event kept a store row in a
  // stale failed state across every reload. Live failures still fire.
  "store_auth_failed",
  "sync_started",
  "sync_progress",
  "sync_complete",
  "sync_failed",
  "sync_cancelled",
  "sync_skipped",
  "post_sync_phase_changed",
  "shortcut_reconcile_complete",
]);

type Handler = (payload: Record<string, unknown>) => void;

/** Wire format returned by `subscribe_replay`. */
interface EventRecord {
  event: string;
  kwargs: Record<string, unknown>;
  timestamp: number;
}

/** Extract the records array from whatever the backend sent —
 *  either a raw list or the `{success, error, data}` envelope.
 *  Returns `[]` for any unexpected shape so callers don't crash. */
function extractRecords(raw: unknown): EventRecord[] {
  // Delegate envelope unwrapping to the shared helper so the
  // semantics stay aligned with `useRPC` / `AuthDispatcher`.
  const unwrapped = unwrapRpcEnvelope<unknown>(raw, {
    route: "subscribe_replay",
    throwing: false,
  });
  if (Array.isArray(unwrapped)) return unwrapped as EventRecord[];
  return [];
}

/** Event bus client impl. */
class EventBusClientImpl {
  private subscribers = new Map<string, Set<Handler>>();
  private lastSeenTimestamp = 0;
  /** False until the first poll after (re)load completes. While false, the
   *  backend's whole replay buffer reads as "fresh", so we skip dispatching
   *  imperative events (see IMPERATIVE_EVENTS) to avoid re-firing stale
   *  side effects from a prior session. */
  private primed = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private currentInterval = POLL_SLOW_MS;
  /** Subscribe to a backend event by name. Returns the
   *  unsubscribe function for cleanup on unmount. */
  subscribe(name: EventName, handler: Handler): () => void {
    let set = this.subscribers.get(name);
    if (!set) {
      set = new Set();
      this.subscribers.set(name, set);
    }
    set.add(handler);
    this.ensurePolling();
    return () => {
      set?.delete(handler);
      if (set?.size === 0) this.subscribers.delete(name);
      if (this.subscribers.size === 0) this.stopPolling();
    };
  }
  /** Bump the polling cadence to fast — call when starting
   *  an operation that will emit events imminently (auth,
   *  install, sync). Auto-decays back to slow once no events
   *  arrive for one tick. */
  bumpToFast(): void {
    this.currentInterval = POLL_FAST_MS;
  }

  /** Dispatch an action URI to the backend. Convenience
   *  wrapper around `dispatch_unifideck_action`. */
  async dispatchAction(verb: string, ...args: string[]): Promise<unknown> {
    const path = args.length
      ? "/" + args.map(encodeURIComponent).join("/")
      : "";
    const uri = `unifideck://${verb}${path}`;
    return call(rpcRoutes.dispatchUnifideckAction, uri);
  }

  /**
   * Start the polling loop if at least one handler
   * is registered and no loop is already running.
   * Idempotent — safe to call from every `on()`.
   */
  private ensurePolling(): void {
    if (this.timer != null) return;
    this.scheduleNext();
  }

  /**
   * Stop the polling loop. Called when the last
   * handler is removed via `off()`. Pending timers
   * are cleared so no late dispatch happens after
   * teardown.
   */
  private stopPolling(): void {
    if (this.timer != null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  /**
   * Arm the next `pollOnce()` invocation. The
   * interval grows under back-pressure (no events
   * received) and resets to the floor on activity,
   * keeping the poll cheap when idle.
   */
  private scheduleNext(): void {
    this.timer = setTimeout(() => {
      void this.pollOnce();
    }, this.currentInterval);
  }

  /**
   * Drain the backend event queue once, fanning
   * each event out to its registered handlers.
   * Network errors are swallowed and logged ; they
   * never propagate to handlers.
   */
  private async pollOnce(): Promise<void> {
    try {
      const raw = await call<[string[]], unknown>(
        rpcRoutes.subscribeReplay,
        WATCHED_EVENTS,
      );
      // Backend wraps every RPC response in `{success, error, data}`
      // via `@auto_wrap_rpc_methods`. `useRPC` unwraps it for
      // component callers, but we call `call()` directly here, so
      // we have to unwrap manually. Tolerate both shapes.
      const records: EventRecord[] = extractRecords(raw);
      // Dedup : keep only events strictly newer than the last
      // we processed. Sort ascending so handlers see them in
      // emission order.
      const fresh = records
        .filter((r) => r.timestamp > this.lastSeenTimestamp)
        .sort((a, b) => a.timestamp - b.timestamp);
      // On the first poll after a (re)load the watermark is 0, so the whole
      // backend replay buffer is "fresh". Most state events are harmless to
      // replay (idempotent UI updates), but two classes are not and must be
      // primed past instead of fired: IMPERATIVE_EVENTS re-run a side effect
      // (RunGame → UPC), and STALE_ON_RELOAD_EVENTS re-animate a sync that
      // already finished (stuck progress bar + repeating restart modal). Both
      // still advance the watermark so they're never seen again; events emitted
      // live during the session fire normally.
      for (const r of fresh) {
        if (
          !this.primed &&
          (IMPERATIVE_EVENTS.has(r.event) ||
            STALE_ON_RELOAD_EVENTS.has(r.event))
        )
          continue;
        this.dispatch(r);
      }
      this.primed = true;
      if (records.length > 0) {
        this.lastSeenTimestamp = Math.max(
          this.lastSeenTimestamp,
          ...records.map((r) => r.timestamp),
        );
      }
      // Adaptive cadence : speed up when events arrive,
      // slow down when the backend is quiet.
      this.currentInterval = fresh.length > 0 ? POLL_FAST_MS : POLL_SLOW_MS;
    } catch (e) {
      console.warn("[EventBusClient] poll error:", e);
      this.currentInterval = POLL_SLOW_MS;
    } finally {
      if (this.subscribers.size > 0) this.scheduleNext();
      else this.timer = null;
    }
  }

  /**
   * Invoke every handler registered for the given
   * event name, isolating each call so a throwing
   * handler cannot break the others. Mirrors the
   * backend EventBus semantics described in the
   * architecture doc §3.2.
   */
  private dispatch(record: EventRecord): void {
    const handlers = this.subscribers.get(record.event);
    if (!handlers) return;
    for (const h of handlers) {
      try {
        h(record.kwargs);
      } catch (e) {
        console.error(`[EventBusClient] handler for ${record.event} threw:`, e);
      }
    }
  }
}

/** Singleton — process-global by nature. */
export const EventBusClient = new EventBusClientImpl();

/** React hook — subscribe to a backend event for the
 *  lifetime of the component. The `deps` array follows the
 *  standard useEffect convention. */
export function useEventBus(
  name: EventName,
  handler: Handler,
  deps: unknown[] = [],
): void {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  useEffect(() => {
    /** Stable. */
    const stable: Handler = (payload) => handlerRef.current(payload);
    return EventBusClient.subscribe(name, stable);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, ...deps]);
}
