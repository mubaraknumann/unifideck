/**
 * useCloudSaveView: what the cloud-save UI should say for one game.
 *
 * One reading of the backend status (`useCloudSaveStatus`) shared by the
 * legacy icon button (`CloudSaveButton`) and the native play bar's status
 * line (`NativeCloudStatusRow`), so both always agree on "in sync / out of
 * sync / syncing / location not found". Also toasts the outcome of a
 * user-started Download / Upload, which arrives later as an event.
 */
import { createElement, useCallback } from "react";
import { showModal } from "@decky/ui";
import { useCloudSaveStatus } from "./useCloudSaveStatus";
import { useToast } from "./useToast";
import { useEventBus } from "../api/event-bus-client";
import { consumeCloudOpPending } from "../api/cloud-save-pending";
import { Events } from "../types/events";
import { toastText, headerText, type ToastKey } from "../services/toast-text";
import { CloudSaveModal } from "../components/modals/CloudSaveModal";

/** Local/cloud save-timestamp delta (seconds) treated as "in sync". Absorbs
 *  gogdl truncating the stored cloud mtime to whole seconds vs the local
 *  file's sub-second st_mtime; a real new save is always far larger. */
const TS_SYNC_TOLERANCE_S = 2;

export type CloudSaveState =
  /** Status not loaded yet. */
  | "checking"
  /** A sync is running. */
  | "syncing"
  /** This game has no cloud saves at all. */
  | "no-support"
  /** We could not find the save folder. */
  | "unresolved"
  /** Local and cloud differ, or only one side exists. */
  | "out-of-sync"
  /** Local and cloud agree. */
  | "in-sync"
  /** Nothing saved on either side yet. */
  | "empty";

export interface CloudSaveView {
  /** False for a store without cloud-save sync: render nothing. */
  visible: boolean;
  state: CloudSaveState;
  /** The cloud has saves this device does not. */
  cloudAvailable: boolean;
  /** Opens the Download / Upload dialog (no-op until the status loads). */
  open: () => void;
  /** The dialog cannot open now. */
  disabled: boolean;
}

function useSyncToasts(store: string, gameId: string): void {
  const toast = useToast();
  const onSyncEvent =
    (dir: "down" | "up", ok: boolean, okKey: ToastKey, failKey: ToastKey) =>
    (p: Record<string, unknown>) => {
      if (p.store !== store || p.game_id !== gameId) return;
      if (!consumeCloudOpPending(store, gameId, dir)) return;
      if (ok) {
        toast.success(headerText("toasts.header.cloudSaves"), toastText(okKey));
        return;
      }
      // A toast has no room for the error; the console keeps it.
      console.warn(`[useCloudSaveView] cloud ${dir} failed:`, p.error);
      toast.error(headerText("toasts.header.cloudSaves"), toastText(failKey));
    };
  const down = ["toasts.cloudPullDone", "toasts.cloudPullFailed"] as const;
  const up = ["toasts.cloudPushDone", "toasts.cloudPushFailed"] as const;
  useEventBus(
    Events.CLOUD_SYNC_DOWN_COMPLETE,
    onSyncEvent("down", true, ...down),
    [store, gameId],
  );
  useEventBus(
    Events.CLOUD_SYNC_DOWN_FAILED,
    onSyncEvent("down", false, ...down),
    [store, gameId],
  );
  useEventBus(Events.CLOUD_SYNC_UP_COMPLETE, onSyncEvent("up", true, ...up), [
    store,
    gameId,
  ]);
  useEventBus(Events.CLOUD_SYNC_UP_FAILED, onSyncEvent("up", false, ...up), [
    store,
    gameId,
  ]);
}

export function useCloudSaveView(
  store: string,
  gameId: string,
  gameTitle: string,
): CloudSaveView {
  const { data, enabled, loading } = useCloudSaveStatus(store, gameId);
  useSyncToasts(store, gameId);

  const open = useCallback(() => {
    if (!data) return;
    showModal(
      createElement(CloudSaveModal, {
        store,
        gameId,
        gameTitle,
        initialStatus: data,
        closeModal: () => {},
      }),
    );
  }, [data, gameId, gameTitle, store]);

  const visible = enabled && !(data && !data.supported);
  const syncing = !!data?.in_progress;
  const noCloudSupport = data?.cloud_supported === false;
  const unresolved = !!data && !data.save_path_resolved;
  const cloudAvailable = data?.has_cloud_saves === true;
  const remote = data?.remote_snapshot;
  const hasLocal = !!data?.has_local_saves;
  const hasCloud = cloudAvailable || !!remote;
  const localTs = data?.local_snapshot?.timestamp;
  const remoteTs = remote?.timestamp;
  // Local vs cloud count as "in sync" within a small tolerance (see
  // TS_SYNC_TOLERANCE_S); a genuine new save is always many seconds newer.
  const diverged =
    hasCloud &&
    hasLocal &&
    !!localTs &&
    !!remoteTs &&
    Math.abs(localTs - remoteTs) > TS_SYNC_TOLERANCE_S;
  const oneSided = hasCloud !== hasLocal;

  let state: CloudSaveState;
  if (!data) state = "checking";
  else if (syncing) state = "syncing";
  else if (noCloudSupport) state = "no-support";
  else if (unresolved) state = "unresolved";
  else if (diverged || oneSided) state = "out-of-sync";
  else if (hasCloud && hasLocal) state = "in-sync";
  else state = "empty";

  return {
    visible,
    state,
    cloudAvailable,
    open,
    disabled: loading || syncing || noCloudSupport || !data,
  };
}
