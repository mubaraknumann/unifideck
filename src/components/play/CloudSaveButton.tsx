/**
 * CloudSaveButton — cloud-save icon button in the Play-section icon row.
 *
 * Surfaces whether a GOG/Epic game has cloud saves and lets the user MANUALLY
 * pull (download) or push (upload) — see {@link CloudSaveModal}. Status is
 * fetched out-of-band via {@link useCloudSaveStatus} so it never blocks the
 * App-Details render. Renders nothing for stores without cloud-save sync.
 *
 * Icon / colour by state:
 *   syncing        → spinning sync icon (disabled)
 *   cloud-available→ download-cloud icon, green tint
 *   no-support     → dim cloud, disabled (game has no cloud saves at all)
 *   unresolved     → cloud, amber tint (save location not found)
 *   default        → plain cloud
 */
import { FC, CSSProperties } from "react";
import { DialogButton, showModal } from "@decky/ui";
import { useTranslation } from "react-i18next";
import {
  FaCheck,
  FaCloud,
  FaCloudDownloadAlt,
  FaSyncAlt,
} from "react-icons/fa";
import { iconBtnStyle, iconBtnClass } from "./PlayMeta";
import { useCloudSaveStatus } from "../../hooks/useCloudSaveStatus";
import { useEventBus } from "../../api/event-bus-client";
import { consumeCloudOpPending } from "../../api/cloud-save-pending";
import { Events } from "../../types/events";
import { useToast } from "../../hooks/useToast";
import {
  toastText,
  headerText,
  type ToastKey,
} from "../../services/toast-text";
import { CloudSaveModal } from "../modals/CloudSaveModal";

interface Props {
  store: string;
  gameId: string;
  gameTitle: string;
}

/** Local/cloud save-timestamp delta (seconds) treated as "in sync". Absorbs
 *  gogdl truncating the stored cloud mtime to whole seconds vs the local
 *  file's sub-second st_mtime; a real new save is always far larger. */
const TS_SYNC_TOLERANCE_S = 2;

/**
 * Cloud glyph with a small check badge — the "local and cloud agree" state.
 *
 * react-icons has no cloud-check in the Fa set, so the badge is composed.
 * Both glyphs are plain `svg`s, so the existing `.unifideck-icon-btn svg`
 * focus rules recolour them together.
 */
const CloudInSyncIcon: FC = () => (
  <span
    style={{
      position: "relative",
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
    }}
  >
    <FaCloud />
    <FaCheck
      className="unifideck-cloud-badge"
      style={{
        position: "absolute",
        right: -4,
        bottom: -3,
        // Punch the badge out of the button fill so it stays legible against
        // the cloud behind it, in both the resting and focused palettes.
        filter: "drop-shadow(0 0 2px rgba(0,0,0,0.9))",
      }}
    />
  </span>
);

export const CloudSaveButton: FC<Props> = ({ store, gameId, gameTitle }) => {
  const { t } = useTranslation();
  const toast = useToast();
  const { data, enabled, loading } = useCloudSaveStatus(store, gameId);

  // Manual Download/Upload are fire-and-forget; their result arrives as a
  // CLOUD_SYNC_* event. Toast the outcome here (the triggering modal has
  // closed) — but ONLY for user-initiated ops (consumeCloudOpPending), so the
  // automatic on-launch pull never spams a toast. (useCloudSaveStatus already
  // refetches the status on these same events.)
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
      console.warn(`[CloudSaveButton] cloud ${dir} failed:`, p.error);
      toast.error(headerText("toasts.header.cloudSaves"), toastText(failKey));
    };
  useEventBus(
    Events.CLOUD_SYNC_DOWN_COMPLETE,
    onSyncEvent("down", true, "toasts.cloudPullDone", "toasts.cloudPullFailed"),
    [store, gameId],
  );
  useEventBus(
    Events.CLOUD_SYNC_DOWN_FAILED,
    onSyncEvent(
      "down",
      false,
      "toasts.cloudPullDone",
      "toasts.cloudPullFailed",
    ),
    [store, gameId],
  );
  useEventBus(
    Events.CLOUD_SYNC_UP_COMPLETE,
    onSyncEvent("up", true, "toasts.cloudPushDone", "toasts.cloudPushFailed"),
    [store, gameId],
  );
  useEventBus(
    Events.CLOUD_SYNC_UP_FAILED,
    onSyncEvent("up", false, "toasts.cloudPushDone", "toasts.cloudPushFailed"),
    [store, gameId],
  );

  // Not a cloud-save store, or the backend says this store isn't supported.
  if (!enabled) return null;
  if (data && !data.supported) return null;

  const syncing = !!data?.in_progress;
  const noCloudSupport = data?.cloud_supported === false;
  const unresolved = !!data && !data.save_path_resolved;

  const local = data?.local_snapshot ?? {};
  const remote = data?.remote_snapshot;
  const cloudAvailable = data?.has_cloud_saves === true;

  const hasLocal = !!data?.has_local_saves;
  const hasCloud = cloudAvailable || !!remote;
  const localTs = local.timestamp;
  const remoteTs = remote?.timestamp;

  const case1 = hasCloud && !hasLocal;
  // Treat local vs cloud as "in sync" within a small tolerance: gogdl stores
  // the cloud mtime (X-Object-Meta-LocalLastModified) truncated to whole
  // seconds, while the local file's st_mtime keeps sub-second precision — so a
  // freshly-pushed save reads ~0.x s apart and must NOT breathe. A genuine new
  // save is always many seconds newer, so 2 s never masks a real divergence.
  const case2and3 =
    hasCloud &&
    hasLocal &&
    !!localTs &&
    !!remoteTs &&
    Math.abs(localTs - remoteTs) > TS_SYNC_TOLERANCE_S;
  const case4 = !hasCloud && hasLocal;

  const shouldBreathe =
    !noCloudSupport && !syncing && (case1 || case2and3 || case4);

  // Local and cloud both exist and their timestamps agree — nothing to do.
  // Worth its own signal: a plain cloud reads as "unknown", which is exactly
  // the ambiguity that made users click through to an empty window.
  const inSync =
    !noCloudSupport &&
    !syncing &&
    !unresolved &&
    hasCloud &&
    hasLocal &&
    !shouldBreathe;

  let icon = <FaCloud />;
  let label = t("play.cloudSave.label");
  let tintBg: string | undefined;
  let tintRing: string | undefined;

  if (syncing) {
    icon = <FaSyncAlt className="unifideck-cloud-spin" />;
    label = t("play.cloudSave.statusSyncing");
    tintRing = "rgba(26, 159, 255, 0.55)";
  } else if (noCloudSupport) {
    label = t("play.cloudSave.noCloudSupport");
  } else if (unresolved) {
    label = t("play.cloudSave.statusUnresolved");
    tintRing = "rgba(244, 180, 0, 0.55)";
  } else if (shouldBreathe) {
    icon = <FaCloudDownloadAlt />;
    label = cloudAvailable
      ? t("play.cloudSave.statusCloudAvailable")
      : t("play.cloudSave.label");
  } else if (inSync) {
    icon = <CloudInSyncIcon />;
    label = t("play.cloudSave.statusInSync");
    tintRing = "rgba(34, 197, 94, 0.55)";
  }

  const style: CSSProperties = {
    ...iconBtnStyle,
    ...(tintBg ? { background: tintBg } : {}),
    ...(tintRing ? { boxShadow: `0 0 0 2px ${tintRing} inset` } : {}),
    ...(noCloudSupport ? { opacity: 0.5 } : {}),
  };

  const open = () => {
    if (!data) return;
    showModal(
      <CloudSaveModal
        store={store}
        gameId={gameId}
        gameTitle={gameTitle}
        initialStatus={data}
        closeModal={() => {}}
      />,
    );
  };

  return (
    <DialogButton
      className={iconBtnClass(
        "unifideck-icon-btn",
        shouldBreathe ? "unifideck-breathe" : "",
      )}
      style={style}
      disabled={loading || syncing || noCloudSupport}
      onClick={open}
      aria-label={label}
    >
      {icon}
    </DialogButton>
  );
};
