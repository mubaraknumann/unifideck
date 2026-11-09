/**
 * DownloadingButtons — Play section variant while a download
 * is active.
 *
 * Single horizontal row inside {@link PlayShell} :
 *
 *   [ Cancel ]   Extracting · 76% · 404 MB / 564 MB · 19.6 MB/s · ETA 00:00:08
 *               ────────────────────  (progress bar)
 *
 * Indeterminate phases (``starting`` / ``extracting`` /
 * ``verifying`` / ``finalizing`` and the rest listed in
 * ``lib/download-phase``) render the slide animation instead of a
 * fractional bar so the user still sees a "working" signal. Cancel
 * opens a ``ConfirmModal`` (destructive) before dispatching the
 * cancel RPC — the staging UX had instant cancel which led to
 * accidental cancellations during animation flashes.
 */
import { FC, useCallback, useState } from "react";
import { DialogButton } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaTimes } from "react-icons/fa";
import { usePlayActions } from "../../hooks/usePlayActions";
import { SteamBridge } from "../../lib/steam-bridge";
import type { DownloadItem } from "../../types/downloads";
import { showsPercent } from "../../lib/download-phase";
import { PlayShell, actionBtnStyle, actionBtnClass } from "./PlayMeta";
import { DownloadProgressRow } from "../downloads/DownloadProgressRow";

interface Props {
  appId: number;
  download: DownloadItem;
  bridge?: SteamBridge;
}

const defaultBridge = new SteamBridge();

export const DownloadingButtons: FC<Props> = ({
  appId,
  download,
  bridge = defaultBridge,
}) => {
  const { t } = useTranslation();
  const play = usePlayActions(appId, bridge);
  const [cancelled, setCancelled] = useState(false);

  const onCancelClick = useCallback(() => {
    if (cancelled) return;
    play.cancelDownload(download, {
      onCancelling: () => setCancelled(true),
      onFailed: () => setCancelled(false),
    });
  }, [cancelled, download, play]);

  return (
    // autoFocus is intentional: claims gamepad focus for the primary action
    // eslint-disable-next-line jsx-a11y/no-autofocus
    <PlayShell autoFocus>
      <DialogButton
        className={actionBtnClass("unifideck-cancel-btn")}
        disabled={cancelled || play.isWorking}
        onClick={onCancelClick}
        style={actionBtnStyle}
      >
        <FaTimes />
        {cancelled
          ? t("play.cancelling")
          : showsPercent(download.download_phase)
          ? `${t("play.cancel")} (${Math.round(
              Math.max(0, Math.min(100, download.progress_percent)),
            )}%)`
          : t("play.cancel")}
      </DialogButton>
      <DownloadProgressRow download={download} marginInlineStart={20} />
    </PlayShell>
  );
};
