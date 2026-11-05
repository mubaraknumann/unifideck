/**
 * DownloadItemRow — single row in the downloads queue.
 *
 * Three variants :
 *  - "current"  : shows progress bar + speed + ETA + cancel
 *  - "queued"   : title + position + cancel-from-queue
 *  - "finished" : title + outcome (success / cancelled /
 *                 error) + remove-from-history
 *
 * The variant is passed by the parent (DownloadsTab) and
 * picks the appropriate rendering. All three variants share
 * the same StoreIcon + title prefix.
 */
import { FC, useMemo } from "react";
import { ButtonItem, DialogButton } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useGameActions } from "../../hooks/useGameActions";
import { useCancelDownload } from "../../hooks/usePlayActions";
import { SteamBridge } from "../../lib/steam-bridge";
import {
  resolveAppIdFromStoreGame,
  getInstalledStatus,
} from "../../lib/library-filters";
import { StoreIcon } from "../shared/StoreIcon";
import { friendlyDownloadError } from "../../lib/download-errors";
import type { DownloadItem, DownloadStatus } from "../../types/downloads";
import { DownloadProgressRow } from "./DownloadProgressRow";

/** Map backend ``DownloadStatus`` to the i18n outcome key
 *  (kept stable across 16 locale files). Backend says
 *  ``"complete"``/``"failed"``/``"running"`` while the i18n
 *  bundle uses ``"completed"``/``"error"``/``"downloading"`` —
 *  the rename would touch every locale, so we adapt instead. */
function outcomeKey(status: DownloadStatus): string {
  if (status === "complete") return "completed";
  if (status === "failed") return "error";
  if (status === "running") return "downloading";
  return status;
}

function outcomeColor(status: DownloadStatus): string {
  // Complete is blue (the Play badge alongside it carries the green
  // "go" accent); cancelled grey; failed red.
  if (status === "complete") return "#1a9fff";
  if (status === "cancelled") return "#94a3b8";
  return "#ef4444";
}

/** Shared badge geometry so the status badge and the Play badge
 *  read as a matched pair. */
const BADGE_STYLE = {
  fontSize: 11,
  padding: "2px 8px",
  borderRadius: 3,
  fontWeight: 600,
  color: "#0f172a",
  lineHeight: "1.4",
} as const;

/** Props. */
interface Props {
  item: DownloadItem;
  variant: "current" | "queued" | "finished";
}

const bridge = new SteamBridge();

/**
 * One row of the downloads tab. Renders the artwork,
 * progress bar, status badge, action buttons. Variants
 * (`current` / `queued` / `finished`) tweak the layout
 * and which actions are exposed.
 */
export const DownloadItemRow: FC<Props> = ({ item, variant }) => {
  const { t } = useTranslation();
  const actions = useGameActions(bridge);
  const cancelDownload = useCancelDownload(actions);

  // Launchable appId for a finished row — only when the download
  // completed AND the game still resolves to an installed shortcut.
  const playAppId = useMemo(() => {
    if (variant !== "finished" || item.status !== "complete") return null;
    const id = resolveAppIdFromStoreGame(item.store, item.game_id);
    // _appType is ignored once the appId is in the cache (which it
    // is — resolveAppIdFromStoreGame read it from the same cache).
    return id != null && getInstalledStatus(id, 0, false) ? id : null;
  }, [variant, item.status, item.store, item.game_id]);

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 6,
        padding: 6,
      }}
    >
      {/* Title row — full width, never competes with badges or
          status text. The QAM panel is narrow, so leaving room
          for the title to lay out on one or two lines (instead of
          wrapping around a sidebar badge) is the readability win. */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          minWidth: 0,
        }}
      >
        <StoreIcon store={item.store} size={14} />
        <span
          style={{
            flex: 1,
            fontWeight: 500,
            minWidth: 0,
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {item.game_title}
        </span>
      </div>

      {variant === "finished" &&
        item.status === "failed" &&
        item.error_message && (
          // Surface WHY it failed — previously only the red badge showed,
          // leaving the user with no explanation (e.g. not enough disk space).
          <span
            style={{
              fontSize: 11,
              color: "#94a3b8",
              lineHeight: "1.4",
              wordBreak: "break-word",
            }}
          >
            {friendlyDownloadError(item.error_message, t)}
          </span>
        )}

      {variant === "finished" && (
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span
            style={{ ...BADGE_STYLE, background: outcomeColor(item.status) }}
          >
            {t(`downloads.outcome.${outcomeKey(item.status)}`)}
          </span>
          {playAppId != null && (
            // Focusable green "Play" badge (replaces the old play icon
            // button) — styled to match the status badge beside it.
            <DialogButton
              className="unifideck-download-play-btn"
              style={{
                ...BADGE_STYLE,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                width: "auto",
                minWidth: 0,
                height: "auto",
                flex: "0 0 auto",
                border: "none",
                background: "#22c55e",
              }}
              onClick={() => actions.launch(playAppId)}
            >
              {t("downloads.play")}
            </DialogButton>
          )}
        </div>
      )}

      {variant === "current" && (
        <>
          <DownloadProgressRow download={item} />
          <ButtonItem
            layout="below"
            disabled={actions.isWorking}
            onClick={() => cancelDownload(item)}
          >
            {t("downloads.cancel")}
          </ButtonItem>
        </>
      )}
      {variant === "queued" && (
        <ButtonItem
          layout="below"
          disabled={actions.isWorking}
          onClick={() => void actions.cancel(item.id)}
        >
          {t("downloads.removeFromQueue")}
        </ButtonItem>
      )}
    </div>
  );
};
