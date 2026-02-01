import { formatETA } from "../../utils/format";
import { formatBytes } from "../../utils/format";
import { FC } from "react";
import { DialogButton, showModal, ConfirmModal } from "@decky/ui";
import {
  FaTimes,
  FaDownload,
  FaCheck,
  FaExclamationTriangle,
} from "react-icons/fa";

import type { DownloadItem } from "../../types/downloads";

import { t } from "../../i18n";
import StoreIcon from "../StoreIcon";

/**
 * Single download item display
 */
const DownloadItemRow: FC<{
  item: DownloadItem;

  onCancel: (id: string) => void;
  onClear?: (id: string) => void;
}> = ({ item, onCancel, onClear }) => {
  const statusColors: Record<string, string> = {
    downloading: "#1a9fff",
    queued: "#888",
    completed: "#4ade80",
    cancelled: "#f59e0b",
    error: "#ef4444",
  };

  return (
    <div
      style={{
        backgroundColor: "#1e2329",
        borderRadius: "8px",
        padding: "12px",
        marginBottom: "8px",
      }}
    >
      {/* Header row: Title + Store + Clear button for finished */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "8px",
        }}
      >
        <div
          style={{ display: "flex", alignItems: "center", flex: 1, gap: "8px" }}
        >
          <StoreIcon store={item.store} size="18px" />
          <span style={{ fontWeight: "bold", color: "#fff", fontSize: "14px" }}>
            {item.game_title}
          </span>
        </div>

        {/* X button to clear finished items */}
        {(item.status === "completed" ||
          item.status === "error" ||
          item.status === "cancelled") &&
          onClear && (
            <>
              <style>{`
                            .unifideck-clear-btn {
                                padding: 0 !important;
                                width: 24px !important;
                                height: 24px !important;
                                min-width: 24px !important;
                                display: flex !important;
                                align-items: center !important;
                                justify-content: center !important;
                                background-color: transparent !important;
                                color: #888 !important;
                                border-radius: 4px !important;
                                transition: all 0.15s ease !important;
                            }
                            .unifideck-clear-btn:hover,
                            .unifideck-clear-btn:focus,
                            .unifideck-clear-btn.gpfocus,
                            .unifideck-clear-btn.Focusable:focus-within {
                                background-color: rgba(255, 255, 255, 0.15) !important;
                                color: #fff !important;
                                outline: 2px solid #1a9fff !important;
                                outline-offset: 1px !important;
                            }
                        `}</style>
              <DialogButton
                className="unifideck-clear-btn"
                onClick={() => onClear(item.id)}
                onOKButton={() => onClear(item.id)}
              >
                <FaTimes size={12} />
              </DialogButton>
            </>
          )}
      </div>

      {/* Progress section (only for downloading) */}
      {item.status === "downloading" && (
        <>
          {/* Show phase-specific messages */}
          {item.download_phase === "extracting" && (
            <div
              style={{
                fontSize: "12px",
                color: "#f59e0b",
                marginBottom: "8px",
              }}
            >
              {item.phase_message ||
                t("downloadsTab.phaseExtracting", { game: item.game_title })}
            </div>
          )}
          {item.download_phase === "verifying" && (
            <div
              style={{
                fontSize: "12px",
                color: "#4ade80",
                marginBottom: "8px",
              }}
            >
              ✓{" "}
              {item.phase_message ||
                t("downloadsTab.phaseVerifying", { game: item.game_title })}
            </div>
          )}

          {/* Show "Preparing..." when no real progress yet */}
          {item.progress_percent === 0 &&
          item.downloaded_bytes === 0 &&
          item.download_phase === "downloading" ? (
            <div
              style={{ fontSize: "12px", color: "#888", fontStyle: "italic" }}
            >
              {t("downloadsTab.preparingDownload")}
            </div>
          ) : item.download_phase === "extracting" ||
            item.download_phase === "verifying" ? (
            /* Animated indeterminate progress bar for extraction/verification */
            <div
              style={{
                width: "100%",
                height: "6px",
                backgroundColor: "#333",
                borderRadius: "3px",
                overflow: "hidden",
                marginBottom: "8px",
              }}
            >
              <div
                style={{
                  width: "30%",
                  height: "100%",
                  backgroundColor:
                    item.download_phase === "extracting"
                      ? "#f59e0b"
                      : "#4ade80",
                  animation: "slide 1.5s ease-in-out infinite",
                }}
              />
              <style>{`
                                @keyframes slide {
                                    0% { transform: translateX(-100%); }
                                    100% { transform: translateX(400%); }
                                }
                            `}</style>
            </div>
          ) : (
            (() => {
              // Calculate progress from bytes when available (more accurate than chunk-based %)
              // Legendary reports chunk-based progress which can differ significantly from byte progress
              const byteProgress =
                item.total_bytes > 0
                  ? (item.downloaded_bytes / item.total_bytes) * 100
                  : item.progress_percent;
              const displayProgress = Math.min(byteProgress, 100);

              return (
                <>
                  {/* Progress bar for downloading */}
                  <div
                    style={{
                      width: "100%",
                      height: "6px",
                      backgroundColor: "#333",
                      borderRadius: "3px",
                      overflow: "hidden",
                      marginBottom: "8px",
                    }}
                  >
                    <div
                      style={{
                        width: `${displayProgress}%`,
                        height: "100%",
                        backgroundColor: "#1a9fff",
                        transition: "width 0.3s ease",
                      }}
                    />
                  </div>

                  {/* Stats row */}
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      flexWrap: "wrap",
                      gap: "4px 8px",
                      fontSize: "12px",
                      color: "#888",
                    }}
                  >
                    <span>{displayProgress.toFixed(1)}%</span>
                    <span>
                      {formatBytes(item.downloaded_bytes)} /{" "}
                      {formatBytes(item.total_bytes)}
                    </span>
                    <span>{item.speed_mbps.toFixed(1)} MB/s</span>
                    <span>ETA: {formatETA(item.eta_seconds)}</span>
                  </div>
                </>
              );
            })()
          )}
        </>
      )}

      {/* Cancel button on new line for active downloads */}
      {(item.status === "downloading" || item.status === "queued") && (
        <div style={{ marginTop: "8px" }}>
          <style>
            {`
                        .cancel-button {
                            padding: 4px 12px;
                            min-width: auto;
                            background-color: #ef4444 !important;
                            color: #fff;
                            font-size: 12px;
                        }
                        .cancel-button:focus,
                        .cancel-button.gpfocus,
                        .cancel-button.Focusable:focus-within {
                            outline: 2px solid #1a9fff !important;
                            outline-offset: 1px !important;
                            color: #fff;
                        }
                    `}
          </style>
          <DialogButton
            onClick={() => {
              showModal(
                <ConfirmModal
                  strTitle={t("downloadsTab.confirmCancelTitle")}
                  strDescription={t("downloadsTab.confirmCancelDescription", {
                    game: item.game_title,
                  })}
                  strOKButtonText={t("downloadsTab.confirmCancelYes")}
                  strCancelButtonText={t("downloadsTab.confirmCancelNo")}
                  bDestructiveWarning={true}
                  onOK={() => onCancel(item.id)}
                />,
              );
            }}
            className="cancel-button"
          >
            {t("downloadsTab.cancelDownload")}
          </DialogButton>
        </div>
      )}

      {/* Status badge for non-downloading items */}
      {item.status !== "downloading" && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            fontSize: "12px",
            color: statusColors[item.status],
          }}
        >
          {item.status === "queued" && (
            <FaDownload size={10} style={{ marginRight: "4px" }} />
          )}
          {item.status === "completed" && (
            <FaCheck size={10} style={{ marginRight: "4px" }} />
          )}
          {item.status === "error" && (
            <FaExclamationTriangle size={10} style={{ marginRight: "4px" }} />
          )}
          <span style={{ textTransform: "capitalize" }}>
            {t(`downloadsTab.status.${item.status}`)}
          </span>
          {item.error_message && (
            <span style={{ marginLeft: "8px", color: "#888" }}>
              - {t(item.error_message)}
            </span>
          )}
        </div>
      )}
    </div>
  );
};

export default DownloadItemRow;
