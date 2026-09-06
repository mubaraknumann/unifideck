/**
 * DownloadProgressRow — rich progress display shared by the
 * App Details custom play section ({@link DownloadingButtons})
 * and the QuickAccess Downloads tab ({@link DownloadItemRow}).
 *
 *   STATUS LABEL  (UPPERCASE · tracked letters)
 *   ──────────────────  (4 px bar, slide-anim while indeterminate)
 *   404 MB / 564 MB         19.6 MB/s · ETA 00:00:08
 *
 * Indeterminate phases (``extracting`` / ``verifying``) hide
 * the byte counter and show the localized phase label (the i18n
 * fallback) so the user still has a textual signal.
 */
import { FC, useEffect } from "react";
import { useTranslation } from "react-i18next";
import type { DownloadItem, DownloadPhase } from "../../types/downloads";
import type { StoreId } from "../../types/api";
import { STORE_VISUALS } from "../../types/store";
import { formatBytes } from "../play/PlayMeta";
import { injectPlayFocusStyles } from "../play/play.css";

interface Props {
  download: DownloadItem;
  /** Inline-start margin applied to the column — caller-controlled
   *  because the play-section variant sits next to a Cancel button
   *  and needs the gap, while the QAM panel doesn't. */
  marginInlineStart?: number;
}

function formatEta(secs: number): string {
  if (!secs || secs <= 0) return "--:--";
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const pad = (n: number) => n.toString().padStart(2, "0");
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function isIndeterminate(phase: DownloadPhase | undefined): boolean {
  return (
    phase === "extracting" ||
    phase === "verifying" ||
    phase === "manual" ||
    phase === "preparing"
  );
}

/**
 * Display name of the vendor client driving a `manual` install.
 *
 * The `manual` phase belongs to every wrapper store, not just Ubisoft, so
 * the label has to name the client the user is actually being sent to — a
 * Battle.net install used to say "Installing Ubisoft Connect". Reads the
 * single source of truth for store branding rather than a second map.
 */
function clientName(store: StoreId | undefined): string {
  return (store && STORE_VISUALS[store]?.display_name) || "";
}

function statusLabelKey(
  status: DownloadItem["status"],
  phase: DownloadPhase | undefined,
  prev: boolean,
): string {
  // Wrapper-store (vendor-client-driven) installs: no real download — show
  // a dedicated label and let the indeterminate path render the detail.
  if (phase === "manual") return "downloadsTab.installingViaClientLabel";
  if (phase === "preparing") return "downloadsTab.preparingLabel";
  if (phase === "extracting") return "downloadsTab.extractingLabel";
  if (phase === "verifying") return "downloadsTab.verifyingLabel";
  if (status === "queued") {
    return prev
      ? "downloadsTab.updateQueuedLabel"
      : "downloadsTab.downloadQueuedLabel";
  }
  return prev
    ? "downloadsTab.downloadingUpdateLabel"
    : "downloadsTab.downloadingLabel";
}

export const DownloadProgressRow: FC<Props> = ({
  download,
  marginInlineStart = 0,
}) => {
  const { t } = useTranslation();
  // The indeterminate slide animation lives in play.css.ts. QAM and the
  // App-Details patch render in different CEF documents, so each one
  // needs its own <style> injection — the helper is idempotent.
  useEffect(() => {
    injectPlayFocusStyles();
  }, []);
  const indeterminate = isIndeterminate(download.download_phase);
  const pct = Math.max(0, Math.min(100, download.progress_percent));
  const prev = Boolean(download.is_update);
  const client = clientName(download.store);
  const label = t(
    statusLabelKey(download.status, download.download_phase, prev),
    { client },
  );
  // Indeterminate detail line: rendered purely from phase (+ percent) so it
  // is always localized. The backend used to also send a ``phase_message``
  // of hardcoded English that was deliberately never displayed; it carried
  // from nine producers through the queue item to this component and was
  // deleted end to end (audit register item 45).
  const phase = download.download_phase;
  const detail =
    phase === "preparing"
      ? t("downloadsTab.preparingMessage")
      : phase === "extracting"
      ? t("downloadsTab.extractingMessage")
      : phase === "verifying"
      ? t("downloadsTab.verifyingMessage", { pct: pct.toFixed(1) })
      : phase === "manual"
      ? t("downloadsTab.installingViaClientMessage", { client })
      : t("downloadsTab.finalizingInstallation");

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 6,
        marginInlineStart,
        flex: "1 1 auto",
        minWidth: 0,
      }}
    >
      <div
        style={{
          fontSize: 11,
          fontWeight: 600,
          textTransform: "uppercase",
          color: "#8f98a0",
          letterSpacing: "0.08em",
        }}
      >
        {label}
      </div>

      <div
        style={{
          height: 4,
          background: "rgba(255, 255, 255, 0.1)",
          borderRadius: 2,
          overflow: "hidden",
          position: "relative",
        }}
      >
        {indeterminate ? (
          <div className="unifideck-progress-indeterminate" />
        ) : (
          <div
            style={{
              height: "100%",
              width: `${pct}%`,
              background: "#1a9fff",
              transition: "width 0.3s ease",
              borderRadius: 2,
            }}
          />
        )}
      </div>

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 11,
          color: "#8f98a0",
          fontWeight: 500,
          letterSpacing: "0.02em",
        }}
      >
        {indeterminate ? (
          <span>{detail}</span>
        ) : (
          <>
            <span>
              {download.total_bytes > 0
                ? `${formatBytes(download.downloaded_bytes)} / ${formatBytes(
                    download.total_bytes,
                  )}`
                : `${pct.toFixed(1)}%`}
            </span>
            {download.status === "running" && (
              <span style={{ marginInlineStart: "auto" }}>
                {t("downloadsTab.speedMbps", {
                  speed: download.speed_mbps.toFixed(1),
                })}
                {" · "}
                {t("downloadsTab.etaLabel", {
                  eta: formatEta(download.eta_seconds),
                })}
              </span>
            )}
          </>
        )}
      </div>
    </div>
  );
};
