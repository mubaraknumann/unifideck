/**
 * boot-event-listener — panel-independent event → toast/modal bridge.
 *
 * Replaces the QAM-bound `<ToastEventListener>` for events that must
 * be handled regardless of whether the Quick Access panel is open:
 *
 *   - `LAUNCHER_STAGE`       → toast or CloudSaveConflictModal
 *   - `SYNC_SKIPPED`         → warning toast explaining the skip
 *   - `STORE_AUTH_COMPLETE`  → navigate to /library/home
 *
 * Started from `definePlugin` and torn down on `onDismount`.
 * Uses the same imperative APIs (`toaster.toast`, `showModal`,
 * `Navigation.Navigate`) that `launcherToasts.tsx` already uses
 * from non-React code, confirmed safe by Decky's `@decky/ui`
 * implementation (showModal resolves the SP window via findSP()).
 */
import { toaster } from "@decky/api";
import { showModal, Navigation } from "@decky/ui";
import i18n from "i18next";
import { EventBusClient } from "../api/event-bus-client";
import { type ToastActionPayload } from "../types/events";
import { CloudSaveConflictModal } from "../components/modals/CloudSaveConflictModal";
import { resolveToastDuration } from "./toast-duration";
import { buildToastParams } from "./toast-params";
import { isConflictAction, resolveToastAction } from "./toast-action";

/** Show a toast via the imperative Decky toaster API. */
function showToast(
  title: string,
  body: string,
  severity?: "info" | "warning" | "error",
  durationMs?: number,
  onClick?: (() => void) | null,
  actionLabel?: string,
): void {
  try {
    toaster.toast({
      title,
      body,
      duration: resolveToastDuration(durationMs, severity),
      ...(onClick ? { onClick } : {}),
      ...(actionLabel ? { subtext: actionLabel } : {}),
    });
  } catch {
    console.log(`[BootEventListener] ${title}: ${body}`);
  }
}

/**
 * `SYNC_SKIPPED.reason` → the i18n key explaining the skip.
 *
 * A skip is an intentional no-op with a user-facing explanation, as distinct
 * from `SYNC_FAILED`. `MicrosoftStore` is the only emitter today: any of these
 * three outcomes drops the entire xCloud library from the sync while the bar
 * still reports success for the other five stores, which reads as "my Game
 * Pass games vanished". The strings existed and were translated in all 16
 * locales the whole time — the event was polled and dropped on the floor
 * (audit §1.3).
 *
 * Keyed by reason rather than by store, and unknown reasons fall through
 * silently, so a future subscription store (EA Play, Ubisoft+) emitting its own
 * reason adds a row here and nothing else.
 */
const SYNC_SKIPPED_KEYS: Record<string, string> = {
  no_active_subscription: "microsoft.syncSkippedNoSubscription",
  subscription_tier_unknown: "microsoft.syncSkippedTierUnknown",
  subscription_check_error: "microsoft.subscriptionCheckFailed",
};

/**
 * Start the boot-time event listener. Returns a cleanup function
 * that unsubscribes all handlers (called from `runTeardown`).
 */
export function startBootEventListener(): () => void {
  const unsubs: (() => void)[] = [];

  // ── LAUNCHER_STAGE ────────────────────────────────────
  unsubs.push(
    EventBusClient.subscribe("launcher_stage", (payload) => {
      const p = payload as ToastActionPayload;
      // `game_title` arrives as a top-level field while the strings
      // interpolate `{{gameTitle}}`; merging it here is what stops every
      // launcher toast rendering with the placeholder unfilled.
      const params = buildToastParams(p);
      const message = p.i18n_key ? String(i18n.t(p.i18n_key, params)) : "";
      if (!message) return;

      // Cloud-save conflict → modal. Discriminated on the SNAPSHOTS, not
      // the verb: `cloud_failure` also sends `retry-sync` for a transient
      // failure with nothing to choose between, and branching on the verb
      // alone would open a pick modal with two empty sides on every dropped
      // Wi-Fi (audit register item 4b).
      if (isConflictAction(p)) {
        const [store, gameId, phase] = p.action?.args ?? [];
        showModal(
          <CloudSaveConflictModal
            gameTitle={String(
              (payload as Record<string, unknown>).game_title ?? gameId,
            )}
            local={
              ((payload as Record<string, unknown>).local_snapshot ??
                {}) as never
            }
            remote={
              ((payload as Record<string, unknown>).remote_snapshot ??
                {}) as never
            }
            onKeepLocal={() => {
              void EventBusClient.dispatchAction(
                "retry-sync",
                store,
                gameId,
                "sync_up",
              );
            }}
            onKeepRemote={() => {
              void EventBusClient.dispatchAction(
                "retry-sync",
                store,
                gameId,
                phase,
              );
            }}
            onCancel={() => {}}
            closeModal={() => {}}
          />,
        );
        return;
      }

      // Generic toast, optionally clickable. Decky toasts take an onClick
      // rather than a button, so the whole toast is the affordance and the
      // action's label goes in the body.
      const onClick = resolveToastAction(p.action);
      const label =
        onClick && p.action?.i18n_label_key
          ? String(i18n.t(p.action.i18n_label_key))
          : "";
      if (p.i18n_title_key) {
        const title = String(i18n.t(p.i18n_title_key, params));
        showToast(title, message, p.severity, p.duration_ms, onClick, label);
      } else {
        showToast(message, label, p.severity, p.duration_ms, onClick, label);
      }
    }),
  );

  // ── SYNC_SKIPPED ──────────────────────────────────────
  unsubs.push(
    EventBusClient.subscribe("sync_skipped", (payload) => {
      const key = SYNC_SKIPPED_KEYS[String(payload.reason)];
      if (!key) return;
      showToast(String(i18n.t(key)), "", "warning");
    }),
  );

  // ── STORE_AUTH_COMPLETE ───────────────────────────────
  unsubs.push(
    EventBusClient.subscribe("store_auth_complete", () => {
      try {
        Navigation.Navigate("/library/home");
      } catch (e) {
        console.error("[BootEventListener] post-auth navigation failed:", e);
      }
    }),
  );

  return () => {
    for (const unsub of unsubs) unsub();
  };
}
