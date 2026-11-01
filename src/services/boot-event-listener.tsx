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
 * Uses the same imperative APIs (`showToast`, `showModal`,
 * `Navigation.Navigate`) that `launcherToasts.tsx` already uses
 * from non-React code, confirmed safe by Decky's `@decky/ui`
 * implementation (showModal resolves the SP window via findSP()).
 */
import { showModal, Navigation } from "@decky/ui";
import { EventBusClient } from "../api/event-bus-client";
import { type ToastActionPayload } from "../types/events";
import { CloudSaveConflictModal } from "../components/modals/CloudSaveConflictModal";
import { resolveToastDuration } from "./toast-duration";
import { isConflictAction } from "./toast-action";
import { showStageToast } from "./stage-toast";
import { toastText, headerText, type ToastKey } from "./toast-text";
import { showToast } from "./show-toast";

/**
 * `SYNC_SKIPPED.reason` → the toast body explaining the skip, under the
 * shared Xbox Library header.
 *
 * A skip is an intentional no-op with a user-facing explanation, as distinct
 * from `SYNC_FAILED`. `MicrosoftStore` is the only emitter today. The first
 * three rows drop the entire xCloud library from the sync while the bar still
 * reports success for the other stores, which reads as "my Game Pass games
 * vanished". The strings existed and were translated in all 16 locales the
 * whole time — the event was polled and dropped on the floor (audit §1.3).
 *
 * The `xbox_pc_*` rows come from the Xbox PC list (`stores/microsoft/
 * pc_library`). Each is sent once until a good read resets it, so a toast
 * here never repeats on every sync.
 *
 * Keyed by reason rather than by store, and unknown reasons fall through
 * silently, so a future subscription store (EA Play, Ubisoft+) emitting its own
 * reason adds a row here and nothing else.
 */
const SYNC_SKIPPED_KEYS: Record<string, ToastKey> = {
  no_active_subscription: "toasts.syncSkipped.noSubscription",
  subscription_tier_unknown: "toasts.syncSkipped.tierUnknown",
  subscription_check_error: "toasts.syncSkipped.checkFailed",
  // Not a whole-store skip: the free titles synced, the Game Pass part did not.
  free_titles_only: "toasts.syncSkipped.freeTitlesOnly",
  // The Xbox PC list could not be refreshed; the last good one was kept.
  xbox_pc_list_kept: "toasts.syncSkipped.xboxPcListKept",
  xbox_pc_signin_needed: "toasts.syncSkipped.xboxPcSignInNeeded",
  xbox_pc_account_changed: "toasts.syncSkipped.xboxPcAccountChanged",
  xbox_pc_installed_unreadable: "toasts.syncSkipped.xboxPcInstalledUnreadable",
  // Sent after `no_active_subscription`: the PC games did sync.
  xbox_pc_only: "toasts.syncSkipped.xboxPcOnly",
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

      showStageToast(p);
    }),
  );

  // ── SYNC_SKIPPED ──────────────────────────────────────
  unsubs.push(
    EventBusClient.subscribe("sync_skipped", (payload) => {
      const key = SYNC_SKIPPED_KEYS[String(payload.reason)];
      if (!key) return;
      showToast({
        title: headerText("toasts.header.xboxLibrary"),
        body: toastText(key),
        duration: resolveToastDuration(undefined, "warning"),
      });
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
