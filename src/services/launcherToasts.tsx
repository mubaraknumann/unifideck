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
import { call } from "@decky/api";
import { showModal } from "@decky/ui";
import { rpcRoutes } from "../api/rpc-routes";
import { unwrapRpcEnvelope } from "../api/useRPC";
import { EventBusClient } from "../api/event-bus-client";
import { CloudSaveConflictModal } from "../components/modals/CloudSaveConflictModal";
import { isConflictAction } from "./toast-action";
import { showStageToast } from "./stage-toast";
import { REPAIR_VERB, repairXboxPcGame } from "./store/xbox-pc-repair";
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

  // Play on an Xbox PC game with missing files: download them now. The
  // toast below still says what is happening.
  if (ev.action?.verb === REPAIR_VERB && ev.action.args?.[0]) {
    void repairXboxPcGame(ev.action.args[0]);
  }

  showStageToast(ev);
}
