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
import { DialogButton } from "@decky/ui";
import { useTranslation } from "react-i18next";
import {
  FaCheck,
  FaCloud,
  FaCloudDownloadAlt,
  FaSyncAlt,
} from "react-icons/fa";
import { iconBtnStyle, iconBtnClass } from "./PlayMeta";
import { useCloudSaveView } from "../../hooks/useCloudSaveView";

interface Props {
  store: string;
  gameId: string;
  gameTitle: string;
}

/**
 * Cloud glyph with a small check badge — the "local and cloud agree" state.
 *
 * react-icons has no cloud-check in the Fa set, so the badge is composed.
 * Both glyphs are plain `svg`s, so the existing `.unifideck-icon-btn svg`
 * focus rules recolour them together.
 */
export const CloudInSyncIcon: FC = () => (
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
  // The status reading (and the Download/Upload result toasts) is shared
  // with the native play bar's cloud line: see useCloudSaveView.
  const view = useCloudSaveView(store, gameId, gameTitle);
  if (!view.visible) return null;

  let icon = <FaCloud />;
  let label = t("play.cloudSave.label");
  let tintRing: string | undefined;
  switch (view.state) {
    case "syncing":
      icon = <FaSyncAlt className="unifideck-cloud-spin" />;
      label = t("play.cloudSave.statusSyncing");
      tintRing = "rgba(26, 159, 255, 0.55)";
      break;
    case "no-support":
      label = t("play.cloudSave.noCloudSupport");
      break;
    case "unresolved":
      label = t("play.cloudSave.statusUnresolved");
      tintRing = "rgba(244, 180, 0, 0.55)";
      break;
    case "out-of-sync":
      icon = <FaCloudDownloadAlt />;
      label = view.cloudAvailable
        ? t("play.cloudSave.statusCloudAvailable")
        : t("play.cloudSave.label");
      break;
    case "in-sync":
      icon = <CloudInSyncIcon />;
      label = t("play.cloudSave.statusInSync");
      tintRing = "rgba(34, 197, 94, 0.55)";
      break;
    default:
      break;
  }

  const style: CSSProperties = {
    ...iconBtnStyle,
    ...(tintRing ? { boxShadow: `0 0 0 2px ${tintRing} inset` } : {}),
    ...(view.state === "no-support" ? { opacity: 0.5 } : {}),
  };

  return (
    <DialogButton
      className={iconBtnClass(
        "unifideck-icon-btn",
        view.state === "out-of-sync" ? "unifideck-breathe" : "",
      )}
      style={style}
      disabled={view.disabled}
      onClick={view.open}
      aria-label={label}
    >
      {icon}
    </DialogButton>
  );
};
