/**
 * DownloadingButtons — Play section variant while a download
 * is active.
 *
 * Single horizontal row inside {@link PlayShell} :
 *
 *   [ Cancel ]   Extracting · 76% · 404 MB / 564 MB · 19.6 MB/s · ETA 00:00:08
 *               ────────────────────  (progress bar)
 *
 * Indeterminate phases (``extracting`` / ``verifying``) render
 * the slide animation instead of a fractional bar so the user
 * still sees a "working" signal. Cancel opens a ``ConfirmModal``
 * (destructive) before dispatching the cancel RPC — the staging
 * UX had instant cancel which led to accidental cancellations
 * during animation flashes.
 */
import { FC, useCallback, useState } from "react";
import { DialogButton, ConfirmModal, showModal } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaTimes } from "react-icons/fa";
import { useGameActions } from "../../hooks/useGameActions";
import { useToast } from "../../hooks/useToast";
import { SteamBridge } from "../../lib/steam-bridge";
import type { DownloadItem } from "../../types/downloads";
import { PlayShell, actionBtnStyle, actionBtnClass } from "./PlayMeta";
import { DownloadProgressRow } from "../downloads/DownloadProgressRow";

interface Props {
  download: DownloadItem;
  bridge?: SteamBridge;
}

const defaultBridge = new SteamBridge();

export const DownloadingButtons: FC<Props> = ({
  download,
  bridge = defaultBridge,
}) => {
  const { t } = useTranslation();
  const actions = useGameActions(bridge);
  const toast = useToast();
  const [cancelled, setCancelled] = useState(false);

  const doCancel = useCallback(async () => {
    setCancelled(true);
    const result = await actions.cancel(download.id);
    if (!result?.success) {
      setCancelled(false);
      toast.error(t("toasts.cancelFailed"), result?.error ?? "");
    }
  }, [actions, download.id, t, toast]);

  const onCancelClick = useCallback(() => {
    if (cancelled) return;
    showModal(
      <ConfirmModal
        strTitle={t("play.cancelConfirmTitle")}
        strDescription={t("play.cancelConfirmBody", {
          title: download.game_title,
        })}
        strOKButtonText={t("play.cancelConfirmConfirm")}
        strCancelButtonText={t("play.cancelConfirmCancel")}
        bDestructiveWarning
        onOK={() => {
          void doCancel();
        }}
      />,
    );
  }, [cancelled, doCancel, download.game_title, t]);

  return (
    // autoFocus is intentional: claims gamepad focus for the primary action
    // eslint-disable-next-line jsx-a11y/no-autofocus
    <PlayShell autoFocus>
      <DialogButton
        className={actionBtnClass("unifideck-cancel-btn")}
        disabled={cancelled || actions.isWorking}
        onClick={onCancelClick}
        style={actionBtnStyle}
      >
        <FaTimes />
        {cancelled
          ? t("play.cancelling")
          : download.download_phase === "manual" ||
            download.download_phase === "preparing" ||
            download.download_phase === "extracting"
          ? t("play.cancel")
          : `${t("play.cancel")} (${Math.round(
              Math.max(0, Math.min(100, download.progress_percent)),
            )}%)`}
      </DialogButton>
      <DownloadProgressRow download={download} marginInlineStart={20} />
    </PlayShell>
  );
};
