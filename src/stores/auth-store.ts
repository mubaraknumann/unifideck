/**
 * auth-store — boot-time singleton for per-store auth status.
 *
 * Absorbs the `prefetchAuthStatus()` logic and the EventBus
 * subscriptions (`STORE_AUTH_COMPLETE`, `STORE_AUTH_FAILED`,
 * `STORE_LOGOUT`) that previously lived inside `<AuthProvider>`.
 *
 * The store starts at boot and keeps auth statuses current even
 * when the QAM panel is closed (e.g. during an OAuth flow that
 * completes in the browser while QAM is dismissed).
 */
import { call } from "@decky/api";
import { rpcRoutes } from "../api/rpc-routes";
import { unwrapRpcEnvelope } from "../api/useRPC";
import { EventBusClient } from "../api/event-bus-client";
import type { StoreId, StoreStatus } from "../types/api";

export type StatusMap = Partial<Record<StoreId, StoreStatus>>;

// ── Snapshot type ────────────────────────────────────────

export interface AuthSnapshot {
  statuses: StatusMap;
  loading: boolean;
}

// ── Helpers ─────────────────────────────────────────────

function parseStatuses(raw: unknown): StatusMap {
  const data = unwrapRpcEnvelope(raw);
  const arr: unknown[] = Array.isArray(data) ? data : [];
  const map: StatusMap = {};
  for (const entry of arr) {
    if (entry && typeof entry === "object") {
      const e = entry as Record<string, unknown>;
      const id = e.store_id as StoreId | undefined;
      if (id) {
        map[id] = e.available ? "connected" : "disconnected";
      }
    }
  }
  return map;
}

// ── Store implementation ────────────────────────────────

type Listener = () => void;

class AuthStoreImpl {
  private _snapshot: AuthSnapshot = { statuses: {}, loading: true };
  private _listeners = new Set<Listener>();
  private _unsubs: (() => void)[] = [];

  /** Start the store — initial fetch + event subscriptions. */
  start(): void {
    // Initial fetch (replaces prefetchAuthStatus)
    void call<[], unknown>(rpcRoutes.checkStoreStatus)
      .then((raw) => {
        this._setSnapshot({
          statuses: parseStatuses(raw),
          loading: false,
        });
      })
      .catch(() => {
        this._setSnapshot({ statuses: {}, loading: false });
      });

    this._unsubs.push(
      EventBusClient.subscribe("store_auth_complete", (payload) => {
        const store = payload.store as StoreId | undefined;
        if (store) this._updateStatus(store, "connected");
      }),
    );

    this._unsubs.push(
      EventBusClient.subscribe("store_auth_failed", (payload) => {
        const store = payload.store as StoreId | undefined;
        if (store) this._updateStatus(store, "error");
      }),
    );

    this._unsubs.push(
      EventBusClient.subscribe("store_logout", (payload) => {
        const store = payload.store as StoreId | undefined;
        if (store) this._updateStatus(store, "disconnected");
      }),
    );
  }

  /** Stop all subscriptions. */
  stop(): void {
    for (const unsub of this._unsubs) unsub();
    this._unsubs = [];
  }

  /** Synchronous status update — called by AuthContext when
   *  useStoreAuth reports success (bypasses EventBus race). */
  notifyConnected(store: StoreId): void {
    this._updateStatus(store, "connected");
  }

  /** Synchronous status update to an arbitrary {@link StoreStatus} — used by
   *  a connection-gated store (Amazon Luna) to reflect a richer backend
   *  verdict than connected/failed, in particular `expired` for a
   *  session-expired re-auth prompt (Req 10.4). Mirrors `notifyConnected`'s
   *  EventBus-race bypass so the panel and Play control update within 2s of
   *  the result landing (Req 10.8). */
  notifyStatus(store: StoreId, status: StoreStatus): void {
    this._updateStatus(store, status);
  }

  /** Clear all statuses (called by logoutAll). */
  clearAll(): void {
    this._setSnapshot({ statuses: {}, loading: false });
  }

  /** Re-fetch statuses from the backend. */
  async refetch(): Promise<void> {
    try {
      const raw = await call<[], unknown>(rpcRoutes.checkStoreStatus);
      this._setSnapshot({
        statuses: parseStatuses(raw),
        loading: false,
      });
    } catch {
      /* best-effort */
    }
  }

  // ── useSyncExternalStore API ──────────────────────────

  getSnapshot = (): AuthSnapshot => this._snapshot;

  subscribe = (listener: Listener): (() => void) => {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  };

  // ── Internals ─────────────────────────────────────────

  private _updateStatus(store: StoreId, status: StoreStatus): void {
    this._setSnapshot({
      statuses: { ...this._snapshot.statuses, [store]: status },
      loading: false,
    });
  }

  private _setSnapshot(next: AuthSnapshot): void {
    this._snapshot = next;
    this._emit();
  }

  private _emit(): void {
    for (const listener of this._listeners) listener();
  }
}

/** Singleton — started at boot from `definePlugin`. */
export const authStore = new AuthStoreImpl();
