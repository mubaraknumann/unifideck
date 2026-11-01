/**
 * The words and numbers shown for a download in progress.
 *
 * Shared by `DownloadProgressRow` (QAM Downloads tab, legacy play row) and
 * the native play bar's status line, so both say exactly the same thing.
 */
import type { TFunction } from "i18next";
import type { DownloadItem } from "../../types/downloads";
import type { StoreId } from "../../types/api";
import { STORE_VISUALS } from "../../types/store";
import {
  detailKey,
  isIndeterminate,
  statusLabelKey,
} from "../../lib/download-phase";
import { formatBytes } from "../play/PlayMeta";

export interface DownloadProgressText {
  /** Status label, e.g. "Downloading" / "Extracting". */
  label: string;
  /** No byte counter yet: show {@link detail} and a sliding bar. */
  indeterminate: boolean;
  /** Localized phase line for indeterminate phases. */
  detail: string;
  /** "404 MB / 564 MB", or "76.0%" when the total is unknown. */
  bytes: string;
  /** "19.6 MB/s · ETA 00:08" while running, else null. */
  rate: string | null;
  /** 0-100. */
  pct: number;
}

export function formatEta(secs: number): string {
  if (!secs || secs <= 0) return "--:--";
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const pad = (n: number) => n.toString().padStart(2, "0");
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/**
 * Display name of the vendor client driving a `manual` install. The
 * `manual` phase belongs to every wrapper store, so the label names the
 * client the user is sent to (a Battle.net install once said "Installing
 * Ubisoft Connect").
 */
function clientName(store: StoreId | undefined): string {
  return (store && STORE_VISUALS[store]?.display_name) || "";
}

export function describeDownload(
  download: DownloadItem,
  t: TFunction,
): DownloadProgressText {
  const pct = Math.max(0, Math.min(100, download.progress_percent));
  const client = clientName(download.store);
  const label = t(
    statusLabelKey(
      download.status,
      download.download_phase,
      Boolean(download.is_update),
    ),
    { client },
  );
  const detail = t(detailKey(download.download_phase), {
    pct: pct.toFixed(1),
    client,
  });
  const bytes =
    download.total_bytes > 0
      ? `${formatBytes(download.downloaded_bytes)} / ${formatBytes(
          download.total_bytes,
        )}`
      : `${pct.toFixed(1)}%`;
  const rate =
    download.status === "running"
      ? `${t("downloadsTab.speedMbps", {
          speed: download.speed_mbps.toFixed(1),
        })} · ${t("downloadsTab.etaLabel", {
          eta: formatEta(download.eta_seconds),
        })}`
      : null;
  return {
    label,
    indeterminate: isIndeterminate(download.download_phase),
    detail,
    bytes,
    rate,
    pct,
  };
}
