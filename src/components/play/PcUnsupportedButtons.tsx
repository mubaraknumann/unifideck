/**
 * PcUnsupportedButtons: Play section for an Xbox PC row that cannot be
 * installed or played as a PC game right now.
 *
 *   [ Not supported yet ]  <reason line>          [ 🎮 ] [ ⚙ ] [ 🗑 ]
 *
 * Reached from `resolvePlaySection` when the row has no xCloud stream to
 * fall back on (a PC-only title), or when the row is installed while the
 * feature is off. The primary button is disabled; the reason line says why
 * (turned off, the status could not be read, EA Play / Ubisoft+ / an
 * unknown plan, or a package choice v1 cannot make). An installed row keeps
 * its Uninstall: uninstalling is never gated on the toggle.
 */
import { FC, useCallback } from "react";
import { DialogButton, showModal } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaBan, FaTrash } from "react-icons/fa";
import { SteamControllerIcon, SteamGearIcon } from "../shared";
import { useGameInfo } from "../../hooks/useGameInfo";
import { useGameActions } from "../../hooks/useGameActions";
import { useToast } from "../../hooks/useToast";
import { downloadErrorToast } from "../../lib/download-errors";
import { toastText, headerText } from "../../services/toast-text";
import { SteamBridge } from "../../lib/steam-bridge";
import {
  pcUnsupportedKey,
  type PcUnsupportedReason,
} from "../../lib/xbox-pc-errors";
import { UninstallConfirmModal } from "../modals/UninstallConfirmModal";
import {
  PlayShell,
  MetaInline,
  IconGroup,
  actionBtnStyle,
  iconBtnStyle,
  actionBtnClass,
  iconBtnClass,
  controllerBtnClass,
} from "./PlayMeta";
import { openControllerConfig, openGearMenu } from "./steamAppDialogs";

interface Props {
  appId: number;
  reason: PcUnsupportedReason;
  installed: boolean;
  bridge?: SteamBridge;
}

const defaultBridge = new SteamBridge();

/** The disabled primary button's label for each kind of reason. */
function buttonKey(reason: PcUnsupportedReason): string {
  if (reason === "disabled") return "xboxPc.unsupported.offButton";
  if (reason === "status_unavailable") {
    return "xboxPc.unsupported.unknownButton";
  }
  return "xboxPc.unsupported.button";
}

/** One muted line saying why the PC version is not available. */
export const PcReasonLine: FC<{ reason: PcUnsupportedReason }> = ({
  reason,
}) => {
  const { t } = useTranslation();
  return (
    <span
      style={{
        fontSize: 12,
        color: "#8f98a0",
        paddingInlineStart: 16,
        paddingInlineEnd: 8,
        maxWidth: 320,
        lineHeight: 1.3,
      }}
    >
      {t(pcUnsupportedKey(reason))}
    </span>
  );
};

export const PcUnsupportedButtons: FC<Props> = ({
  appId,
  reason,
  installed,
  bridge = defaultBridge,
}) => {
  const { t } = useTranslation();
  const { data: game, loading } = useGameInfo(appId);
  const actions = useGameActions(bridge);
  const toast = useToast();

  const onUninstall = useCallback(() => {
    if (!game) return;
    showModal(
      <UninstallConfirmModal
        gameId={appId}
        gameTitle={game.title}
        onConfirm={async (deletePrefix) => {
          const r = await actions.uninstall(appId, deletePrefix);
          if (r.success) {
            toast.success(
              headerText("toasts.header.uninstall"),
              toastText("toasts.uninstallDone"),
            );
          } else {
            toast.error(
              headerText("toasts.header.uninstall"),
              downloadErrorToast(r.error),
            );
          }
        }}
        closeModal={() => {}}
      />,
    );
  }, [actions, appId, game, toast]);

  return (
    // autoFocus is intentional: claims gamepad focus for the primary action
    // eslint-disable-next-line jsx-a11y/no-autofocus
    <PlayShell autoFocus>
      <DialogButton
        className={actionBtnClass("unifideck-unsupported-btn")}
        disabled
        style={actionBtnStyle}
      >
        <FaBan /> {t(buttonKey(reason))}
      </DialogButton>
      <PcReasonLine reason={reason} />
      {installed && (
        <MetaInline
          sizeBytes={game?.size_bytes}
          showLastPlayed
          appId={appId}
          store={game?.store}
          gameId={game?.id}
          installed
        />
      )}
      <IconGroup>
        <DialogButton
          className={controllerBtnClass()}
          style={iconBtnStyle}
          onClick={() => openControllerConfig(appId)}
          aria-label={t("playButton.controllerConfig")}
        >
          <SteamControllerIcon />
        </DialogButton>
        <DialogButton
          className={iconBtnClass()}
          style={iconBtnStyle}
          onClick={(e) => openGearMenu(e?.currentTarget as HTMLElement, appId)}
          aria-label={t("playButton.appSettings")}
        >
          <SteamGearIcon />
        </DialogButton>
        {installed && (
          <DialogButton
            className={iconBtnClass()}
            style={iconBtnStyle}
            disabled={loading || actions.isWorking}
            onClick={onUninstall}
            aria-label={t("play.uninstall")}
          >
            <FaTrash />
          </DialogButton>
        )}
      </IconGroup>
    </PlayShell>
  );
};
