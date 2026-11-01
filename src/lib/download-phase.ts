/**
 * download-phase — what a running download row says for each queue phase.
 *
 * Shared by `DownloadProgressRow` (the Downloads tab and the App Details
 * play section) and `DownloadingButtons` (its Cancel button). Pure, so the
 * phase-to-wording mapping is testable without rendering React.
 *
 * Each helper returns a translation key, not text. The caller translates it
 * and passes the `pct` and `client` values the keys interpolate.
 *
 * A phase is only reused where its wording is true. The Xbox PC license
 * check once rode on `preparing`, so a download that had not fetched a byte
 * told the user it was "Setting up game… Initializing prefix and syncing
 * cloud saves…".
 */
import type { DownloadItem, DownloadPhase } from "../types/downloads";

/** Phases with no byte counter: the bar slides and a detail line shows. */
export function isIndeterminate(phase: DownloadPhase | undefined): boolean {
  return (
    phase === "starting" ||
    phase === "extracting" ||
    phase === "verifying" ||
    phase === "finalizing" ||
    phase === "manual" ||
    phase === "preparing"
  );
}

/**
 * Whether the Cancel button carries the percentage. Of the indeterminate
 * phases only `verifying` has a real one, which its detail line shows too.
 * In the others the number sits at 0% or 100% and says nothing.
 */
export function showsPercent(phase: DownloadPhase | undefined): boolean {
  return phase === "verifying" || !isIndeterminate(phase);
}

/** Key of the row's upper-case status label. */
export function statusLabelKey(
  status: DownloadItem["status"],
  phase: DownloadPhase | undefined,
  prev: boolean,
): string {
  // Wrapper-store (vendor-client-driven) installs have no real download.
  // They get a dedicated label and the indeterminate path renders the detail.
  if (phase === "manual") return "downloadsTab.installingViaClientLabel";
  if (phase === "starting") return "downloadsTab.preparingDownload";
  if (phase === "preparing") return "downloadsTab.preparingLabel";
  if (phase === "extracting") return "downloadsTab.extractingLabel";
  if (phase === "verifying") return "downloadsTab.verifyingLabel";
  if (phase === "finalizing") return "downloadsTab.finalizingLabel";
  if (status === "queued") {
    return prev
      ? "downloadsTab.updateQueuedLabel"
      : "downloadsTab.downloadQueuedLabel";
  }
  return prev
    ? "downloadsTab.downloadingUpdateLabel"
    : "downloadsTab.downloadingLabel";
}

/**
 * Key of the detail line an indeterminate phase shows in place of the byte
 * counter. `finalizing` takes the last branch, as does any phase this
 * version does not know.
 */
export function detailKey(phase: DownloadPhase | undefined): string {
  if (phase === "starting") return "downloadsTab.startingMessage";
  if (phase === "preparing") return "downloadsTab.preparingMessage";
  if (phase === "extracting") return "downloadsTab.extractingMessage";
  if (phase === "verifying") return "downloadsTab.verifyingMessage";
  if (phase === "manual") return "downloadsTab.installingViaClientMessage";
  return "downloadsTab.finalizingInstallation";
}
