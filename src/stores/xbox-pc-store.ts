/**
 * xbox-pc-store: boot-time cache of the Xbox PC status snapshot.
 *
 * Modelled on `store-info-store`. The store:
 *
 *   1. Fetches `get_xbox_pc_status` once at boot (cache only on the
 *      backend, no subprocess).
 *   2. Replaces the snapshot on every `xbox_pc_state_changed` event. The
 *      payload is the whole status, so there is nothing to merge.
 *   3. Gives up waiting after {@link STATUS_TIMEOUT_MS} and reports
 *      `status_unavailable`. A backend failure must not read as "the
 *      feature is off" (rule 8), so `error` is kept apart from `enabled`.
 *   4. Wraps the action RPCs, and applies any status they answer with.
 *
 * It also toasts the end of a component install. That lives here, not in
 * the QAM row, because the install runs for minutes and the QAM is usually
 * closed by the time it ends.
 *
 * React components subscribe via `useSyncExternalStore`
 * (`hooks/useXboxPcStatus`). `getSnapshot` returns the same object until
 * something changes, or the hook would loop.
 */
import { call, toaster } from "@decky/api";
import i18n from "i18next";
import { rpcRoutes, type RouteName } from "../api/rpc-routes";
import { EventBusClient } from "../api/event-bus-client";
import { Events } from "../types/events";
import { xboxPcErrorText } from "../lib/xbox-pc-errors";
import type { XboxPcStatus } from "../types/api";

// ── Snapshot type ────────────────────────────────────────

export interface XboxPcSnapshot {
  status: XboxPcStatus | null;
  loading: boolean;
  /** An error code (`status_unavailable`), never the feature being off. */
  error: string | null;
}

/** An action RPC's outcome: the payload, or the backend's error code. */
export type XboxPcCallResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string };

/** How long boot waits for a first status before reporting an error. */
export const STATUS_TIMEOUT_MS = 8000;

/** The error code shown when no status could be read. */
export const STATUS_UNAVAILABLE = "status_unavailable";

/** Runtime install start/cancel answers. Never a top-level `success`. */
export interface RuntimeInstallAnswer {
  started?: boolean;
  cancelled?: boolean;
  reason?: string | null;
  error?: string | null;
}

// ── Wire helpers ────────────────────────────────────────

/** Whether `v` has the fields every consumer reads. */
export function isXboxPcStatus(v: unknown): v is XboxPcStatus {
  if (!v || typeof v !== "object") return false;
  const s = v as Record<string, unknown>;
  return (
    typeof s.available === "boolean" &&
    typeof s.enabled === "boolean" &&
    typeof s.runtime === "object" &&
    s.runtime !== null &&
    typeof s.account === "object" &&
    s.account !== null
  );
}

/**
 * Call `route` and read the backend envelope by hand.
 *
 * `useRPC` would throw away the error code, and `unwrapRpcEnvelope` with
 * `throwing: false` returns null for it. The code (`notice_not_acknowledged`,
 * `edge_busy`, ...) is the whole point here, so it is kept.
 */
export async function callXboxPc<T>(
  route: RouteName,
  ...args: unknown[]
): Promise<XboxPcCallResult<T>> {
  let raw: unknown;
  try {
    raw = await call<unknown[], unknown>(route, ...args);
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
  if (raw && typeof raw === "object" && "success" in raw) {
    const env = raw as { success: boolean; error?: string | null; data?: T };
    if (!env.success) return { ok: false, error: env.error || "unknown" };
    return { ok: true, data: env.data as T };
  }
  return { ok: true, data: raw as T };
}

function sameStatus(a: XboxPcStatus | null, b: XboxPcStatus | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return JSON.stringify(a) === JSON.stringify(b);
}

/** A toast through Decky; a no-op where the toaster is missing (tests). */
function notify(title: string, body: string): void {
  try {
    toaster.toast({ title, body, duration: 7000 });
  } catch (e) {
    console.warn("[XboxPcStore] toast failed:", e);
  }
}

// ── Store implementation ────────────────────────────────

type Listener = () => void;

class XboxPcStoreImpl {
  private _snapshot: XboxPcSnapshot = {
    status: null,
    loading: true,
    error: null,
  };
  private _listeners = new Set<Listener>();
  private _unsubs: (() => void)[] = [];
  private _timer: ReturnType<typeof setTimeout> | null = null;

  /** Start the store: initial fetch, the event, and the boot timeout. */
  start(): void {
    this.stop();
    this._timer = setTimeout(() => {
      this._timer = null;
      if (this._snapshot.status == null) {
        this._set({ status: null, loading: false, error: STATUS_UNAVAILABLE });
      }
    }, STATUS_TIMEOUT_MS);
    this._unsubs.push(
      EventBusClient.subscribe(Events.XBOX_PC_STATE_CHANGED, (payload) => {
        this.apply(payload);
      }),
    );
    void this._fetch(rpcRoutes.getXboxPcStatus);
  }

  /** Stop the event subscription and the boot timer. */
  stop(): void {
    for (const unsub of this._unsubs) unsub();
    this._unsubs = [];
    if (this._timer != null) clearTimeout(this._timer);
    this._timer = null;
  }

  /** Re-read the cached status (cheap; no subprocess). */
  async refetch(): Promise<void> {
    await this._fetch(rpcRoutes.getXboxPcStatus);
  }

  /** Ask the backend to re-check the account, then read the status. */
  async refresh(): Promise<void> {
    await this._fetch(rpcRoutes.refreshXboxPcStatus);
  }

  /**
   * Replace the snapshot with `status` if it is a valid one.
   *
   * Used for the event and for every RPC that answers with a status. A
   * malformed payload is ignored rather than wiping a good snapshot.
   */
  apply(status: unknown): void {
    if (!isXboxPcStatus(status)) return;
    const prev = this._snapshot.status;
    if (sameStatus(prev, status) && this._snapshot.error == null) return;
    this._set({ status, loading: false, error: null });
    this._announceRuntime(prev, status);
  }

  // ── Actions ───────────────────────────────────────────

  /** Turn the feature on or off. `noticeVersion` acknowledges the notice. */
  async setEnabled(
    enabled: boolean,
    noticeVersion?: number,
  ): Promise<XboxPcCallResult<XboxPcStatus>> {
    const args = noticeVersion == null ? [enabled] : [enabled, noticeVersion];
    return this._statusCall(rpcRoutes.setXboxPcEnabled, ...args);
  }

  /** Sign the Xbox PC account out (the xCloud sign-in is kept). */
  async signOut(): Promise<XboxPcCallResult<XboxPcStatus>> {
    return this._statusCall(rpcRoutes.signOutXboxPc);
  }

  /** Start the component install. Progress arrives as events. */
  async installRuntime(): Promise<XboxPcCallResult<RuntimeInstallAnswer>> {
    const r = await callXboxPc<RuntimeInstallAnswer>(
      rpcRoutes.installXboxPcRuntime,
    );
    if (!r.ok) return r;
    const refusal = r.data?.reason || r.data?.error;
    if (r.data?.started === false && refusal) {
      return { ok: false, error: refusal };
    }
    return r;
  }

  /** Cancel a running component install. */
  async cancelRuntimeInstall(): Promise<
    XboxPcCallResult<RuntimeInstallAnswer>
  > {
    return callXboxPc<RuntimeInstallAnswer>(
      rpcRoutes.cancelXboxPcRuntimeInstall,
    );
  }

  // ── useSyncExternalStore API ──────────────────────────

  getSnapshot = (): XboxPcSnapshot => this._snapshot;

  subscribe = (listener: Listener): (() => void) => {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  };

  // ── Internals ─────────────────────────────────────────

  private async _statusCall(
    route: RouteName,
    ...args: unknown[]
  ): Promise<XboxPcCallResult<XboxPcStatus>> {
    const r = await callXboxPc<XboxPcStatus>(route, ...args);
    if (r.ok) this.apply(r.data);
    return r;
  }

  private async _fetch(route: RouteName): Promise<void> {
    const r = await callXboxPc<XboxPcStatus>(route);
    if (r.ok && isXboxPcStatus(r.data)) {
      this.apply(r.data);
      return;
    }
    if (!r.ok) console.warn(`[XboxPcStore] ${route} failed:`, r.error);
    // Keep a good status we already have; only a store with nothing to
    // show reports the error.
    if (this._snapshot.status == null) {
      this._set({ status: null, loading: false, error: STATUS_UNAVAILABLE });
    }
  }

  /** Toast the end of a component install (see the module docstring). */
  private _announceRuntime(
    prev: XboxPcStatus | null,
    next: XboxPcStatus,
  ): void {
    if (prev?.runtime.state !== "installing") return;
    if (next.runtime.state === "installed") {
      notify(
        i18n.t("toasts.xboxPcRuntimeInstalled"),
        i18n.t("toasts.xboxPcRuntimeInstalledBody"),
      );
    } else if (next.runtime.state === "failed") {
      notify(
        i18n.t("toasts.xboxPcRuntimeFailed"),
        xboxPcErrorText(
          next.runtime.error,
          i18n.t.bind(i18n),
          next.runtime.source_dir,
        ),
      );
    }
  }

  private _set(next: XboxPcSnapshot): void {
    this._snapshot = next;
    for (const listener of this._listeners) listener();
  }
}

/** Singleton, started at boot from `definePlugin`. */
export const xboxPcStore = new XboxPcStoreImpl();

/** A fresh, unstarted store. For tests only. */
export function _createXboxPcStore(): XboxPcStoreImpl {
  return new XboxPcStoreImpl();
}
