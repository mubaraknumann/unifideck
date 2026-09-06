/**
 * Turn a backend toast `action` into something the user can click.
 *
 * Decky toasts take an `onClick`, not a button, so the whole toast becomes
 * the affordance and `i18n_label_key` names it in the body.
 *
 * Shared by both renderers — `boot-event-listener` (plugin bus) and
 * `launcherToasts` (launcher bridge) — which previously each special-cased
 * `retry-sync` and dropped every other verb on the floor. That was harmless
 * only because the sole producer of other verbs (`cloud_failure`) was itself
 * unimported; once cloud-sync failures started surfacing (audit register item
 * 37) the dropped verbs became real. Audit register item 4b.
 *
 * **`retry-sync` has two producers with different intent**, which is the trap
 * here and the reason this helper does not own that verb:
 *
 * - `CloudSaveService._emit_save_conflict` sends it **with**
 *   `local_snapshot` / `remote_snapshot` — a genuine local-vs-cloud
 *   divergence, where the user must *choose*, so it opens
 *   `CloudSaveConflictModal`.
 * - `cloud_failure` sends it **without** snapshots for a transient failure
 *   (network unreachable, timeout, 5xx) — there is nothing to choose, the
 *   right action is simply to try again.
 *
 * Branching on the verb alone would open a conflict modal with two empty
 * snapshots on every dropped Wi-Fi. The callers discriminate on the
 * snapshots; see `isConflictAction`.
 */
import { EventBusClient } from "../api/event-bus-client";

export interface ToastAction {
  verb: string;
  args: string[];
  i18n_label_key?: string;
}

/** Payload fields that mark a `retry-sync` as a real conflict to resolve. */
interface MaybeConflict {
  local_snapshot?: unknown;
  remote_snapshot?: unknown;
  action?: { verb?: string };
}

/**
 * True when this payload is a cloud-save conflict needing the pick modal,
 * as opposed to a plain retry.
 */
export function isConflictAction(payload: MaybeConflict): boolean {
  return (
    payload.action?.verb === "retry-sync" &&
    (payload.local_snapshot != null || payload.remote_snapshot != null)
  );
}

function openExternal(url: string): void {
  const steam = (
    window as unknown as {
      SteamClient?: { URL?: { ExecuteSteamURL?: (u: string) => void } };
    }
  ).SteamClient;
  steam?.URL?.ExecuteSteamURL?.(url);
}

/**
 * The click handler for `action`, or `null` when there is nothing to do.
 *
 * Fails closed: an unknown verb yields `null` so the toast renders as a
 * plain message rather than a button that does nothing. A dead affordance
 * is worse than none — the user presses it, nothing happens, and they learn
 * to distrust the next one.
 */
export function resolveToastAction(
  action: ToastAction | undefined | null,
): (() => void) | null {
  if (!action?.verb) return null;

  // `open-url` carries an external target plus an optional fallback, so one
  // action shape covers both a backend verb and a Steam deep link without a
  // second field to branch on.
  if (action.verb === "open-url") {
    const [target, fallback] = action.args ?? [];
    if (!target) return null;
    return () => {
      try {
        openExternal(target);
      } catch {
        if (fallback) openExternal(fallback);
      }
    };
  }

  // Everything else is a `unifideck://` verb the backend dispatches.
  if (!KNOWN_DISPATCH_VERBS.has(action.verb)) return null;
  return () => {
    void EventBusClient.dispatchAction(action.verb, ...(action.args ?? []));
  };
}

/**
 * Verbs `dispatch_unifideck_action` accepts.
 *
 * Deliberately a closed set rather than "dispatch whatever arrives": an
 * unrecognised verb round-trips to the backend only to be rejected, and the
 * user sees a toast that does nothing. `open-save-folder` and `show-logs`
 * are **absent** — they are frontend verbs whose modals do not exist, so
 * offering them would be exactly that dead affordance.
 */
const KNOWN_DISPATCH_VERBS = new Set([
  "auth",
  "retry-sync",
  "refresh-library",
  "refresh-all-libraries",
]);
