/**
 * NotInstalledButtons — Play section variant for an
 * uninstalled Unifideck game.
 *
 * Single horizontal row inside {@link PlayShell} :
 *
 *   [ Install ]   Space Required: 564 MB           [ 🎮 ] [ ⚙ ]
 *
 * Install delegates to ``useInstallFlow.start(game)`` which
 * runs the storage picker + GOG language modal before
 * queueing the download.
 *
 * A merged Xbox PC + xCloud row passes ``streamUrl``: a secondary Stream
 * button then sits beside Install (Install stays primary).
 */
import { FC } from "react";
import { DialogButton } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaDownload } from "react-icons/fa";
import { SteamControllerIcon, SteamGearIcon } from "../shared";
import { useGameInfo } from "../../hooks/useGameInfo";
import { usePlayActions } from "../../hooks/usePlayActions";
import { SteamBridge } from "../../lib/steam-bridge";
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
import { StreamSecondaryButton } from "./StreamSecondaryButton";

interface Props {
  appId: number;
  bridge?: SteamBridge;
  /** Set on a merged Xbox PC + xCloud row: show a secondary Stream. */
  streamUrl?: string;
}

const defaultBridge = new SteamBridge();

export const NotInstalledButtons: FC<Props> = ({
  appId,
  bridge = defaultBridge,
  streamUrl,
}) => {
  const { t } = useTranslation();
  const { data: game, loading } = useGameInfo(appId);
  const play = usePlayActions(appId, bridge);
  const onInstall = play.install;

  return (
    // autoFocus is intentional: claims gamepad focus for the primary action
    // eslint-disable-next-line jsx-a11y/no-autofocus
    <PlayShell autoFocus>
      <DialogButton
        className={actionBtnClass("unifideck-install-btn")}
        disabled={loading || play.isWorking || !game}
        onClick={onInstall}
        style={actionBtnStyle}
      >
        <FaDownload />
        {play.isWorking ? t("playButton.installing") : t("playButton.install")}
      </DialogButton>
      {streamUrl && <StreamSecondaryButton appId={appId} url={streamUrl} />}

      <MetaInline
        sizeBytes={game?.size_bytes}
        showLastPlayed
        appId={appId}
        store={game?.store}
        gameId={game?.id}
      />

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
      </IconGroup>
    </PlayShell>
  );
};
