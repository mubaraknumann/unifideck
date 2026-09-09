/**
 * launcherToasts — persistent launcher-toast poll.
 *
 * The game launcher runs as a separate process and appends its
 * LAUNCHER_STAGE toast events to a shared bridge file (backend
 * `launcher.frontend_bridge`). This poll drains them via the
 * `get_launcher_toasts` RPC and surfaces them.
 *
 * It is started from `definePlugin` and runs independently of the QAM
 * panel, because launches happen with the Unifideck panel CLOSED in
 * Gaming Mode — and the QAM-bound `ToastEventListener` only polls while
 * the panel is open, so it never sees launch-time toasts (first-time
 * prefix setup, dependency install, Proton switch, …). This poll is the
 * panel-independent delivery path for everything the launcher emits.
 */
import { call, toaster } from "@decky/api";
import { showModal } from "@decky/ui";
import i18n from "i18next";
import { rpcRoutes } from "../api/rpc-routes";
import { unwrapRpcEnvelope } from "../api/useRPC";
import { EventBusClient } from "../api/event-bus-client";
import { CloudSaveConflictModal } from "../components/modals/CloudSaveConflictModal";
import { resolveToastDuration } from "./toast-duration";
import { buildToastParams } from "./toast-params";
import { isConflictAction, resolveToastAction } from "./toast-action";
import type { ToastActionPayload } from "../types/events";

const POLL_INTERVAL_MS = 2000;

/**
 * One toast row from the launcher bridge file.
 *
 * Extends the canonical `ToastActionPayload` rather than restating it. This
 * interface used to redeclare every field, including a **third** copy of the
 * `action` shape — so the payload contract lived in three places
 * (`core/types/events.py`, `types/events.ts`, and here) and adding a field
 * to one silently left the others behind. Exactly the class audit §1.4 flags
 * as "frontend event names maintained in three places".
 *
 * The two snapshot fields are local because only the cloud-save conflict
 * path sends them.
 */
interface LauncherToast extends ToastActionPayload {
  local_snapshot?: Record<string, unknown>;
  remote_snapshot?: Record<string, unknown>;
}

/**
 * Start the launcher-toast poll. Returns a stop function for teardown.
 */
export function startLauncherToastPoll(): () => void {
  let stopped = false;

  const tick = async (): Promise<void> => {
    let events: LauncherToast[];
    try {
      const raw = await call<[], unknown>(rpcRoutes.getLauncherToasts);
      events = unwrapRpcEnvelope<LauncherToast[]>(raw, {
        route: rpcRoutes.getLauncherToasts,
        throwing: false,
      });
    } catch {
      return; // best-effort — launcher toasts are non-critical
    }
    if (!Array.isArray(events)) return;
    for (const ev of events) showLauncherToast(ev);
  };

  const timer = setInterval(() => {
    if (!stopped) void tick();
  }, POLL_INTERVAL_MS);

  return () => {
    stopped = true;
    clearInterval(timer);
  };
}

function showLauncherToast(ev: LauncherToast): void {
  // `game_title` is a top-level payload field, but the strings interpolate
  // it as `{{gameTitle}}` — so without merging it in here every launcher
  // toast rendered with the placeholder unfilled ("Starting  through
  // Battle.net…"). An explicit i18n_params entry still wins, since a caller
  // that named the variable meant it.
  const params = buildToastParams(ev);

  // Cloud-save conflict → modal so the user can pick keep-local/remote.
  // Discriminated on the SNAPSHOTS, not the verb: `cloud_failure` also sends
  // `retry-sync` for a transient failure with nothing to choose between
  // (audit register item 4b).
  if (isConflictAction(ev)) {
    const [store, gameId, phase] = ev.action?.args ?? [];
    showModal(
      <CloudSaveConflictModal
        gameTitle={String(ev.game_title ?? gameId)}
        local={(ev.local_snapshot ?? {}) as never}
        remote={(ev.remote_snapshot ?? {}) as never}
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

  const message = ev.i18n_key ? String(i18n.t(ev.i18n_key, params)) : "";
  if (!message && !ev.i18n_title_key) return;

  // Match the QAM ToastEventListener: title key (when present) is the
  // bold header and i18n_key is the body; otherwise the message is the
  // title with no body.
  const title = ev.i18n_title_key
    ? String(i18n.t(ev.i18n_title_key, params))
    : message;
  const body = ev.i18n_title_key ? message : "";

  // Decky toasts take an onClick, not a button — so the whole toast is the
  // affordance and the action's label goes in the subtext.
  const onClick = resolveToastAction(ev.action);
  const actionLabel =
    onClick && ev.action?.i18n_label_key
      ? String(i18n.t(ev.action.i18n_label_key))
      : "";

  try {
    toaster.toast({
      title,
      body,
      duration: resolveToastDuration(ev.duration_ms, ev.severity),
      ...(onClick ? { onClick } : {}),
      ...(actionLabel ? { subtext: actionLabel } : {}),
    });
  } catch {
    console.log(`[LauncherToast] ${title}: ${body}`);
  }
}
