/**
 * CloudSaveModal — manual cloud-save control opened from the
 * cloud-save icon button.
 *
 * Shows the Local vs Cloud snapshot (file count / size / time) and two
 * deliberate actions: "Download cloud save" (force pull) and "Upload local
 * save" (push). Upload is the destructive direction, so it is manual by
 * default and still passes through all backend `safety.py` wipe guards — a
 * regression surfaces the existing CloudSaveConflictModal via the toast poll.
 *
 * The window STAYS OPEN after a Download/Upload: it subscribes to its own
 * {@link useCloudSaveStatus} (live), so the Local/Cloud rows repopulate in
 * place when the background sync finishes — the user sees the result without
 * the modal vanishing on them.
 *
 * When the save location couldn't be auto-detected, the window offers an
 * inline folder picker that writes the per-game `games.<id>.save_path`
 * override (the highest-priority resolution tier).
 */
import { FC, useState } from "react";
import { ConfirmModal, DialogButton, Focusable } from "@decky/ui";
import { useTranslation } from "react-i18next";
import {
  FaCloud,
  FaCloudDownloadAlt,
  FaCloudUploadAlt,
  FaDesktop,
  FaExclamationTriangle,
  FaFolderOpen,
  FaSyncAlt,
  FaUndo,
} from "react-icons/fa";
import { rpcRoutes } from "../../api/rpc-routes";
import { useRPCMutation } from "../../api/useRPC";
import { EventBusClient, useEventBus } from "../../api/event-bus-client";
import { markCloudOpPending } from "../../api/cloud-save-pending";
import { Events } from "../../types/events";
import { useToast } from "../../hooks/useToast";
import { toastText, headerText } from "../../services/toast-text";
import {
  useCloudSaveStatus,
  type CloudSaveStatus,
  type SaveSnapshot,
} from "../../hooks/useCloudSaveStatus";
import { StoragePathPicker } from "./StoragePathPicker";

interface SyncResult {
  success?: boolean;
  error?: string | null;
}

interface Props {
  store: string;
  gameId: string;
  gameTitle: string;
  /** Status the button already fetched — shown until the live hook refreshes. */
  initialStatus: CloudSaveStatus;
  closeModal: () => void;
}

function formatTs(ts: number | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatBytes(n: number | undefined): string {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = n;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(i > 0 && value < 10 ? 1 : 0)} ${units[i]}`;
}

const rowStyle = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
} as const;

const actionBtnStyle = {
  minWidth: 180,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 8,
} as const;

export const CloudSaveModal: FC<Props> = ({
  store,
  gameId,
  gameTitle,
  initialStatus,
  closeModal,
}) => {
  const { t } = useTranslation();
  const toast = useToast();
  // The modal owns a live status query so the Local/Cloud rows refresh in
  // place when the background sync completes. Seed from the status the button
  // already fetched to avoid a loading flash on open.
  const live = useCloudSaveStatus(store, gameId);
  const status = live.data ?? initialStatus;

  const pull = useRPCMutation<[string, string, boolean], SyncResult>(
    rpcRoutes.cloudSavePull,
  );
  const push = useRPCMutation<[string, string], SyncResult>(
    rpcRoutes.cloudSavePush,
  );
  const setPath = useRPCMutation<[string, string, string], unknown>(
    rpcRoutes.setGameSavePath,
  );
  const [picking, setPicking] = useState(false);
  // Local "just clicked" flag bridges the gap until the backend lock shows up
  // in a refetch; cleared when a CLOUD_SYNC_* event for this game arrives.
  const [triggered, setTriggered] = useState(false);

  const clearTriggered = (p: Record<string, unknown>) => {
    if (p.store === store && p.game_id === gameId) setTriggered(false);
  };
  useEventBus(Events.CLOUD_SYNC_DOWN_COMPLETE, clearTriggered, [store, gameId]);
  useEventBus(Events.CLOUD_SYNC_DOWN_FAILED, clearTriggered, [store, gameId]);
  useEventBus(Events.CLOUD_SYNC_UP_COMPLETE, clearTriggered, [store, gameId]);
  useEventBus(Events.CLOUD_SYNC_UP_FAILED, clearTriggered, [store, gameId]);

  const syncing = triggered || !!status.in_progress;
  const busy = syncing || pull.loading || push.loading;

  const local: Partial<SaveSnapshot> = status.local_snapshot ?? {};
  const remote = status.remote_snapshot;

  // Fire-and-forget: the backend runs the sync in the background and returns
  // immediately, so the RPC never times out on a long download. We DON'T close
  // — the live hook refetches on the CLOUD_SYNC_* event and the rows update in
  // place. The CloudSaveButton shows the result toast (markCloudOpPending
  // marks this as a user-initiated op so the auto-pull never toasts).
  const onDownload = async () => {
    markCloudOpPending(store, gameId, "down");
    setTriggered(true);
    toast.info(
      headerText("toasts.header.cloudSaves"),
      toastText("toasts.cloudPullStarted"),
    );
    await pull.mutate(store, gameId, true);
    EventBusClient.bumpToFast();
    void live.refetch();
  };

  const onUpload = async () => {
    markCloudOpPending(store, gameId, "up");
    setTriggered(true);
    toast.info(
      headerText("toasts.header.cloudSaves"),
      toastText("toasts.cloudPushStarted"),
    );
    await push.mutate(store, gameId);
    EventBusClient.bumpToFast();
    void live.refetch();
  };

  const onPickFolder = async (path: string) => {
    setPicking(false);
    const r = await setPath.mutate(store, gameId, path);
    if (r !== null) {
      toast.success(
        headerText("toasts.header.cloudSaves"),
        toastText("toasts.cloudSavePathSet"),
      );
      void live.refetch();
    } else {
      toast.error(
        headerText("toasts.header.cloudSaves"),
        toastText("toasts.cloudSavePathFailed"),
      );
    }
  };

  // Empty path clears `games.<id>.save_path` backend-side, dropping back to
  // strategy + prefix auto-detection.
  const onResetPath = async () => {
    const r = await setPath.mutate(store, gameId, "");
    if (r !== null) {
      toast.success(
        headerText("toasts.header.cloudSaves"),
        toastText("toasts.cloudSavePathReset"),
      );
      void live.refetch();
    } else {
      toast.error(
        headerText("toasts.header.cloudSaves"),
        toastText("toasts.cloudSavePathFailed"),
      );
    }
  };

  // Both directions need a REAL resolved save location — without one (no
  // prefix yet) there's nowhere the game reads from, so we don't sync into a
  // staging dir. The unresolved banner guides the user to launch / set a path.
  const canDownload =
    !busy && status.save_path_resolved && status.cloud_supported !== false;
  const canUpload =
    !busy && status.save_path_resolved && status.has_local_saves;

  // Build a snapshot line from ONLY the fields we actually know (0 = unknown).
  // The cloud size often isn't available from the store's listing — showing
  // nothing is better than an inaccurate (mirror-derived) value.
  const describeSnap = (snap: Partial<SaveSnapshot>): string => {
    const parts: string[] = [];
    if (snap.file_count)
      parts.push(t("play.cloudSave.filesCount", { count: snap.file_count }));
    if (snap.total_bytes) parts.push(formatBytes(snap.total_bytes));
    if (snap.timestamp) parts.push(formatTs(snap.timestamp));
    return parts.length ? parts.join(" · ") : t("play.cloudSave.noCloudData");
  };

  // Inline folder picker mode — replaces the snapshot panel.
  if (picking) {
    return (
      <ConfirmModal
        strTitle={t("play.cloudSave.setLocationTitle")}
        strDescription={t("play.cloudSave.setLocationHint")}
        bAlertDialog
        strOKButtonText={t("common.cancel")}
        onOK={() => setPicking(false)}
        onCancel={() => setPicking(false)}
      >
        <StoragePathPicker
          startPath={status.browse_start || "/home"}
          onConfirm={(path) => void onPickFolder(path)}
        />
      </ConfirmModal>
    );
  }

  return (
    <ConfirmModal
      strTitle={t("play.cloudSave.menuTitle", { game: gameTitle })}
      strDescription=""
      bAlertDialog
      strOKButtonText={t("play.cloudSave.close")}
      onOK={closeModal}
      // Without onCancel, B does nothing and the window can only be dismissed
      // with the on-screen Close button. Steam's own bAlertDialog call sites
      // always pass both handlers.
      onCancel={closeModal}
    >
      <div style={{ padding: "10px 0" }}>
        {!status.save_path_resolved && (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 8,
              marginBottom: 14,
              padding: 10,
              borderRadius: 8,
              background: "rgba(244, 180, 0, 0.08)",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                color: "#ffc107",
                fontSize: 13,
              }}
            >
              <FaExclamationTriangle size={18} />
              <span>{t("play.cloudSave.statusUnresolved")}</span>
            </div>
            <span style={{ fontSize: 12, color: "#bbb" }}>
              {t("play.cloudSave.unresolvedHint")}
            </span>
            <DialogButton
              onClick={() => setPicking(true)}
              style={{
                ...actionBtnStyle,
                minWidth: 0,
                alignSelf: "flex-start",
              }}
            >
              <FaFolderOpen /> {t("play.cloudSave.setLocation")}
            </DialogButton>
          </div>
        )}
        {status.save_path_resolved && (
          // A RESOLVED path can still be the WRONG path — auto-detection
          // sometimes lands on the whole game folder. Previously the picker
          // was reachable only from the unresolved banner above, so a
          // mis-detected location had no route to a fix. Always offer it.
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 8,
              marginBottom: 14,
              padding: 10,
              borderRadius: 8,
              background: "rgba(255,255,255,0.04)",
            }}
          >
            <span style={{ fontSize: 12, color: "#bbb" }}>
              {t("play.cloudSave.currentLocation")}
            </span>
            <span
              style={{
                fontSize: 12,
                color: "#ddd",
                wordBreak: "break-all",
                lineHeight: "1.4",
              }}
            >
              {status.save_path}
            </span>
            <Focusable
              flow-children="row"
              style={{ display: "flex", gap: 8, flexWrap: "wrap" }}
            >
              <DialogButton
                onClick={() => setPicking(true)}
                style={{ ...actionBtnStyle, minWidth: 0 }}
              >
                <FaFolderOpen /> {t("play.cloudSave.changeLocation")}
              </DialogButton>
              {status.save_path_is_override && (
                <DialogButton
                  disabled={setPath.loading}
                  onClick={() => void onResetPath()}
                  style={{ ...actionBtnStyle, minWidth: 0 }}
                >
                  <FaUndo /> {t("play.cloudSave.resetLocation")}
                </DialogButton>
              )}
            </Focusable>
          </div>
        )}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 10,
            marginBottom: 18,
            backgroundColor: "rgba(255,255,255,0.05)",
            padding: 15,
            borderRadius: 8,
          }}
        >
          <div style={rowStyle}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <FaDesktop size={16} /> <span>{t("cloudSave.local")}</span>
            </div>
            <span style={{ fontSize: 12, color: "#aaa" }}>
              {status.has_local_saves
                ? describeSnap(local)
                : t("play.cloudSave.statusNoSaves")}
            </span>
          </div>
          <div style={rowStyle}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <FaCloud size={16} /> <span>{t("cloudSave.cloud")}</span>
            </div>
            <span style={{ fontSize: 12, color: "#aaa" }}>
              {remote
                ? describeSnap(remote)
                : status.cloud_supported === false
                ? t("play.cloudSave.noCloudSupport")
                : t("play.cloudSave.noCloudData")}
            </span>
          </div>
        </div>
        {/* A bare flex div creates NO gamepad nav node, so Download/Upload
            ended up as flat siblings of Close under the column-flow
            DialogBody — left/right did nothing and DOWN moved between two
            side-by-side buttons. Steam's own two-button dialog rows are
            Focusables for exactly this reason. */}
        <Focusable
          flow-children="row"
          style={{
            display: "flex",
            gap: 10,
            justifyContent: "center",
            marginBottom: 6,
          }}
        >
          <DialogButton
            disabled={!canDownload}
            onClick={onDownload}
            style={actionBtnStyle}
          >
            <FaCloudDownloadAlt /> {t("play.cloudSave.download")}
          </DialogButton>
          <DialogButton
            disabled={!canUpload}
            onClick={onUpload}
            style={actionBtnStyle}
          >
            <FaCloudUploadAlt /> {t("play.cloudSave.upload")}
          </DialogButton>
        </Focusable>
        {syncing && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 8,
              fontSize: 12,
              color: "#1a9fff",
              marginTop: 8,
            }}
          >
            <FaSyncAlt className="unifideck-cloud-spin" />
            {t("play.cloudSave.statusSyncing")}
          </div>
        )}
        {!syncing && status.auto_pull && (
          <div
            style={{
              fontSize: 11,
              color: "rgba(255,255,255,0.45)",
              textAlign: "center",
              marginTop: 4,
            }}
          >
            {t("play.cloudSave.autoPullOn")}
          </div>
        )}
      </div>
    </ConfirmModal>
  );
};
