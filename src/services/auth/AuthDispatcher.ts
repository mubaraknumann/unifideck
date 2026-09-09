/**
 * AuthDispatcher — frontend-side auth orchestrator.
 *
 * Owns the full per-store auth handshake end-to-end :
 *
 *   1. ``store_auth(store, "start")`` on the backend so it
 *      creates/refreshes the auth shortcut, writes its
 *      auth-URL file, starts its session monitor, etc.
 *   2. ``launch<Store>AuthViaShortcut()`` so the user actually
 *      sees the browser / launcher (Steam's ``RunGame()``).
 *   3. Subscribes to the backend EventBus
 *      (``STORE_AUTH_COMPLETE`` / ``STORE_AUTH_FAILED``) so
 *      callers can `await` the final outcome.
 *
 * Components / hooks call ``AuthDispatcher.start(store)`` and
 * get a single Promise back. The legacy
 * ``dispatch_unifideck_action("unifideck://auth/<store>")``
 * channel is kept as the way the *backend* talks to itself
 * from toast-action buttons ; UI-initiated connects use the
 * direct ``store_auth`` RPC because that's the only path that
 * returns the per-store ``AuthResult`` we toast on completion.
 *
 * Mutex : only one auth flow at a time. A second `start()`
 * for the same store while another is in flight returns the
 * in-flight promise ; for a different store, it rejects.
 */
import { call } from "@decky/api";
import { EventBusClient } from "../../api/event-bus-client";
import { rpcRoutes } from "../../api/rpc-routes";
import { unwrapRpcEnvelope } from "../../api/useRPC";
import {
  type ShortcutLaunchResult,
  watchAppStopped,
} from "../../lib/steam-bridge/shortcut-types";
import { Events } from "../../types/events";
import {
  launchAmazonAuthViaShortcut,
  launchEpicAuthViaShortcut,
  launchGogAuthViaShortcut,
  launchMicrosoftAuthViaShortcut,
} from "../../utils/authShortcutLaunch";
import { launchUbisoftAuthViaShortcut } from "../../utils/ubisoftShortcutLaunch";
import { launchBattlenetAuthViaShortcut } from "../../utils/battlenetShortcutLaunch";
import { prepareForSync } from "../../lib/steam-bridge/prepare-sync";
import { storeReportsConnected } from "./store-status";
import type { StoreId, AuthResult } from "../../types/api";

const AUTH_TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes ceiling

/** How long a second press is treated as a double-click on the same flow
 *  rather than a deliberate retry. Anything longer is the user telling us
 *  the previous attempt is dead — see {@link AuthDispatcherImpl.start}. */
const DEDUPE_WINDOW_MS = 3000;

/** Grace after the auth app exits before the flow is called failed. The
 *  backend monitor polls the auth prefix every 2s and the vendor client
 *  flushes its token as it shuts down, so a successful sign-in's
 *  ``STORE_AUTH_COMPLETE`` routinely lands *after* the app has stopped. */
const AUTH_APP_STOPPED_GRACE_MS = 20 * 1000;

/** Auth event payload. */
interface AuthEventPayload {
  store?: string;
  success?: boolean;
  error?: string;
}

/** Backend `store_auth` envelope (unwrapped by useRPC for hook
 *  callers ; we call `call()` directly here, so the wrapper
 *  envelope is left intact and unwrapped manually). */
interface StoreAuthResponse {
  success?: boolean;
  data?: AuthResult;
  url?: string;
  error?: string;
}

/** What the two-stage kick learned: an immediate verdict, if the backend
 *  had one, and the appid Steam was asked to run, if a shortcut launched. */
interface KickOutcome {
  early: AuthResult | null;
  appId?: number;
}

/** One in-flight auth flow, plus what it takes to abandon it. */
interface InflightAuth {
  promise: Promise<AuthResult>;
  startedAt: number;
  /** Settle this flow early so a fresh attempt can take over. */
  supersede: (reason: string) => void;
}

/** Auth dispatcher impl. */
class AuthDispatcherImpl {
  /** Per-store in-flight auth: allows concurrent auth for
   *  DIFFERENT stores (e.g. user starts GOG then clicks
   *  Microsoft — both run in parallel). Only deduplicates
   *  the SAME store, and only briefly — see {@link start}. */
  private inflight = new Map<StoreId, InflightAuth>();

  /** Start the auth flow for `store`. Resolves when the
   *  backend emits `STORE_AUTH_COMPLETE` / `STORE_AUTH_FAILED`
   *  for that store, or rejects on timeout / shortcut launch
   *  failure.
   *
   *  A second press is only deduplicated inside
   *  {@link DEDUPE_WINDOW_MS}. Past that it supersedes the old
   *  flow instead of returning it, because this map is a
   *  module singleton and handing back a stale pending promise
   *  is indistinguishable from a dead button: the press makes
   *  no RPC and launches nothing. A store whose backend never
   *  emitted a terminal event therefore stayed unusable until
   *  Steam was restarted (which is simply what rebuilds this
   *  singleton). Reported from a Battle.net sign-in that ended
   *  on a rejected login. */
  async start(store: StoreId): Promise<AuthResult> {
    const existing = this.inflight.get(store);
    if (existing) {
      if (Date.now() - existing.startedAt < DEDUPE_WINDOW_MS) {
        return existing.promise;
      }
      console.log(
        `[AuthDispatcher:${store}] superseding a previous sign-in attempt`,
      );
      existing.supersede("superseded by a new sign-in attempt");
    }

    EventBusClient.bumpToFast();
    const { promise, supersede } = this.runFlow(store);
    const entry: InflightAuth = { promise, startedAt: Date.now(), supersede };
    this.inflight.set(store, entry);
    promise
      .finally(() => {
        // Only clear our own entry: ``supersede`` settles the old promise,
        // whose ``finally`` runs as a microtask *after* the replacement has
        // been stored, and an unguarded delete would drop the live one.
        if (this.inflight.get(store) === entry) this.inflight.delete(store);
      })
      // The derived promise is otherwise unhandled, so a rejected flow
      // (timeout, launch failure) surfaced as an unhandled rejection.
      .catch(() => {});
    return promise;
  }

  /**
   * Internal coroutine that owns one auth flow end-to-end :
   *  - subscribe to STORE_AUTH_COMPLETE / STORE_AUTH_FAILED
   *  - kick the backend `store_auth` RPC
   *  - launch the auth shortcut so the user sees the flow
   *  - resolve / reject + dispose every listener.
   */
  private runFlow(store: StoreId): {
    promise: Promise<AuthResult>;
    supersede: (reason: string) => void;
  } {
    // Assigned synchronously by the Promise executor below, which runs
    // before this function returns.
    let supersede: (reason: string) => void = () => {};
    const promise = new Promise<AuthResult>((resolve, reject) => {
      /** Cleanup. */
      const cleanup: Array<() => void> = [];

      /** Timer. */
      const timer = setTimeout(() => {
        for (const fn of cleanup) fn();
        reject(new Error(`auth timeout: ${store}`));
      }, AUTH_TIMEOUT_MS);

      cleanup.push(() => clearTimeout(timer));

      supersede = (reason: string): void => {
        for (const fn of cleanup) fn();
        resolve({ success: false, store, error: reason });
      };

      /** On resolved. */
      const onResolved = (result: AuthResult): void => {
        for (const fn of cleanup) fn();
        if (result.success) {
          // Fire-and-forget: a fresh login should make the store
          // available for sync immediately, not just after the next
          // restart. Queues behind an in-flight sync on the backend
          // (SyncService._enqueue) rather than blocking this Promise —
          // callers resolve as soon as auth completes, same as before.
          //
          // ``prepareForSync`` first, and awaited inside the chain rather
          // than skipped: this path used to fire the RPC bare while the
          // user-triggered syncs in ``SyncContext`` did three preparatory
          // steps. That gap was the whole difference between a manual sync,
          // which worked, and the automatic one at login, which did not. See
          // ``lib/steam-bridge/prepare-sync`` for what each step is for.
          void prepareForSync()
            .then(() =>
              call<[StoreId], unknown>(rpcRoutes.requestAuthSync, store),
            )
            .catch((e) => {
              console.error(
                `[AuthDispatcher:${store}] requestAuthSync failed:`,
                e,
              );
            });
        }
        resolve(result);
      };

      cleanup.push(
        EventBusClient.subscribe(Events.STORE_AUTH_COMPLETE, (raw) => {
          const p = raw as AuthEventPayload;
          if (p.store !== store) return;
          onResolved({ success: true, store });
        }),
      );

      cleanup.push(
        EventBusClient.subscribe(Events.STORE_AUTH_FAILED, (raw) => {
          const p = raw as AuthEventPayload;
          if (p.store !== store) return;
          onResolved({
            success: false,
            store,
            error: p.error ?? "unknown auth failure",
          });
        }),
      );

      /** Settle once the launched auth app has exited.
       *
       *  The backend emitting a terminal event is necessary but not
       *  sufficient: any store whose emitter is missing, or whose event
       *  is lost, would otherwise hold this promise — and with it the
       *  button — for the full timeout. The app's own exit is a signal
       *  we always get, so it is the backstop.
       *
       *  The grace expiring is NOT by itself a failure. A wrapper store
       *  writes its session as the client shuts down, and the backend
       *  only clears its signed-out marker once the post-capture hook has
       *  run, so a genuinely successful sign-in can still be mid-flight
       *  here. Declaring failure on the timer alone reported a completed
       *  sign-in as failed and sent the user straight back into another
       *  launch. So ask the backend what it actually thinks first — the
       *  same probe the stores tab uses — and only fail if it agrees. */
      const onAuthAppStopped = (): void => {
        console.log(
          `[AuthDispatcher:${store}] auth app stopped; waiting ` +
            `${AUTH_APP_STOPPED_GRACE_MS}ms for a verdict`,
        );
        const grace = setTimeout(() => {
          void storeReportsConnected(store).then((connected) => {
            if (connected) {
              console.log(
                `[AuthDispatcher:${store}] no event, but the backend ` +
                  `reports the store connected; treating as success`,
              );
              onResolved({ success: true, store });
              return;
            }
            onResolved({
              success: false,
              store,
              error: "sign-in was closed before it completed",
            });
          });
        }, AUTH_APP_STOPPED_GRACE_MS);
        cleanup.push(() => clearTimeout(grace));
      };

      // Fire the kick + shortcut launch only after the
      // listeners are installed — otherwise a fast backend
      // flow could emit its terminal event before we
      // subscribe.
      void this.kickAndLaunch(store)
        .then(({ early, appId }) => {
          // Fast-path : the backend's ``store_auth`` returned
          // ``success: true`` right away (already-authed user).
          // Don't wait for an EventBus echo — the event may
          // race the RPC response and arrive before our poll
          // tick, leaving the Promise hung. Resolve directly.
          if (early) {
            onResolved(early);
            return;
          }
          if (appId !== undefined) {
            cleanup.push(watchAppStopped(appId, onAuthAppStopped));
          }
        })
        .catch((e) => {
          for (const fn of cleanup) fn();
          reject(e);
        });
    });
    return { promise, supersede };
  }

  /** Two-stage kick : backend prep then frontend shortcut
   *  launch.
   *
   *  Returns an ``AuthResult`` only on the **fast path** —
   *  i.e. when ``store_auth`` reports ``success: true``
   *  immediately (the store's fast-path detected existing
   *  valid tokens). In that case the caller resolves the
   *  Promise directly with the returned result : the
   *  backend's ``STORE_AUTH_COMPLETE`` event may race the
   *  RPC response (the EventBus polls every 250-2000ms), so
   *  waiting for it would hang the UI at "Working…".
   *
   *  Returns ``null`` on the slow path — the shortcut has
   *  been kicked, and the surrounding ``runFlow`` waits for
   *  ``STORE_AUTH_COMPLETE`` / ``STORE_AUTH_FAILED`` events
   *  to resolve.
   *
   *  Throws if either stage fails outright (so ``runFlow``
   *  rejects the Promise).
   */
  private async kickAndLaunch(store: StoreId): Promise<KickOutcome> {
    console.log(`[AuthDispatcher:${store}] backend prep via store_auth`);
    const raw = await call<[StoreId, string], unknown>(
      rpcRoutes.storeAuth,
      store,
      "start",
    );
    const startResult = unwrapRpcEnvelope<StoreAuthResponse>(raw, {
      route: rpcRoutes.storeAuth,
      throwing: false,
    });
    console.log(`[AuthDispatcher:${store}] store_auth returned:`, startResult);
    if (
      startResult?.success === true &&
      !(startResult as { metadata?: { pending?: boolean } } | null)?.metadata
        ?.pending
    ) {
      console.log(
        `[AuthDispatcher:${store}] backend reports already-authed, ` +
          `resolving without shortcut launch`,
      );
      return { early: { success: true, store } };
    }
    // Backend reported a structured failure (e.g. ``edge_not_installed``)
    // before any shortcut was needed. Surface it as the resolved
    // AuthResult so the caller (useStoreAuth) can react — show the
    // Chromium install modal, surface a toast, etc. — instead of
    // firing a useless shortcut launch the user can't complete.
    if (startResult && startResult.success === false && startResult.error) {
      console.log(
        `[AuthDispatcher:${store}] backend rejected start: ` +
          `${startResult.error} — skipping shortcut launch`,
      );
      return {
        early: {
          success: false,
          store,
          error: startResult.error,
        } as AuthResult,
      };
    }
    console.log(`[AuthDispatcher:${store}] launching shortcut`);
    const launchResult = await this.launchForStore(store);
    console.log(
      `[AuthDispatcher:${store}] shortcut launch result:`,
      launchResult,
    );
    if (!launchResult.success) {
      throw new Error(
        launchResult.error ?? `${store} auth shortcut failed to launch`,
      );
    }
    // Slow path : shortcut launched, wait for the backend's terminal event
    // to land on the EventBus — or, failing that, for the launched app to
    // stop, which is why the appid comes back with the result.
    return { early: null, appId: launchResult.app_id };
  }

  /** Dispatch to the per-store shortcut launcher. */
  private async launchForStore(store: StoreId): Promise<ShortcutLaunchResult> {
    switch (store) {
      case "epic":
        return launchEpicAuthViaShortcut();
      case "gog":
        return launchGogAuthViaShortcut();
      case "amazon":
        return launchAmazonAuthViaShortcut();
      case "microsoft":
        return launchMicrosoftAuthViaShortcut();
      case "ubisoft":
        return launchUbisoftAuthViaShortcut();
      case "battlenet":
        return launchBattlenetAuthViaShortcut();
      default:
        return { success: false, error: `no launcher wired for ${store}` };
    }
  }
}
/** Singleton — auth flows are mutually exclusive by nature. */
export const AuthDispatcher = new AuthDispatcherImpl();
