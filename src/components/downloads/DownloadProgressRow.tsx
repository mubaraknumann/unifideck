/**
 * DownloadProgressRow — rich progress display shared by the
 * App Details custom play section ({@link DownloadingButtons})
 * and the QuickAccess Downloads tab ({@link DownloadItemRow}).
 *
 *   STATUS LABEL  (UPPERCASE · tracked letters)
 *   ──────────────────  (4 px bar, slide-anim while indeterminate)
 *   404 MB / 564 MB         19.6 MB/s · ETA 00:00:08
 *
 * Indeterminate phases (``starting``, ``extracting``, ``verifying``,
 * ``finalizing`` and the others in ``lib/download-phase``) hide the
 * byte counter and show a localized detail line so the user still
 * has a textual signal.
 */
import { FC, useEffect } from "react";
import { useTranslation } from "react-i18next";
import type { DownloadItem } from "../../types/downloads";
import { injectPlayFocusStyles } from "../play/play.css";
import { describeDownload } from "./download-progress-text";

interface Props {
  download: DownloadItem;
  /** Inline-start margin applied to the column — caller-controlled
   *  because the play-section variant sits next to a Cancel button
   *  and needs the gap, while the QAM panel doesn't. */
  marginInlineStart?: number;
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
  // The words and numbers are shared with the native play bar's status
  // line (see download-progress-text.ts), so both always agree.
  const { label, indeterminate, detail, bytes, rate, pct } = describeDownload(
    download,
    t,
  );

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
            <span>{bytes}</span>
            {rate && <span style={{ marginInlineStart: "auto" }}>{rate}</span>}
          </>
        )}
      </div>
    </div>
  );
};
