/**
 * InstalledButtons — Play section variant for an installed
 * Unifideck game.
 *
 * Single horizontal row inside {@link PlayShell} :
 *
 *   Idle:    [ Play ]     Space Required · Last Played      [ 🎮 ] [ ⚙ ] [ ✕ ]
 *   Running: [ Resume ] [ ✕ ]   Space Required · Last Played [ 🎮 ] [ ⚙ ] [ ✕ ]
 *   Update:  [ Update ]  Space Required · Last Played      [ 🎮 ] [ ⚙ ] [ ✕ ]
 *
 * Running detection is ``useAppRunning`` (Steam's per-client
 * ``display_status``, polled every 2 s). Update detection is a
 * reactive read of ``UpdateStore`` via ``useGameUpdate`` — the
 * backend sweep fills it, so no per-mount RPC fires here.
 */
import { FC, useCallback } from "react";
import { DialogButton } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaPlay, FaSyncAlt, FaTimes, FaTrash } from "react-icons/fa";
import { SteamControllerIcon, SteamGearIcon } from "../shared";
import { useAppRunning } from "../../hooks/useAppRunning";
import { useGameInfo } from "../../hooks/useGameInfo";
import { useGameActions } from "../../hooks/useGameActions";
import { usePlayActions } from "../../hooks/usePlayActions";
import { SteamBridge } from "../../lib/steam-bridge";
import { CloudSaveButton } from "./CloudSaveButton";
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
  bridge?: SteamBridge;
}

const defaultBridge = new SteamBridge();
export const InstalledButtons: FC<Props> = ({
  appId,
  bridge = defaultBridge,
}) => {
  const { t } = useTranslation();
  const { data: game, loading } = useGameInfo(appId);
  const actions = useGameActions(bridge);
  const play = usePlayActions(appId, bridge);
  const isRunning = useAppRunning(appId);
  // Read-only view of the backend sweep's result, already in memory, so it
  // cannot delay the button (see usePlayActions for the prompts and toasts).
  const hasUpdate = play.hasUpdate;

  // NOTE: we deliberately do NOT touch Steam's Force-Compatibility here.
  // ``config.vdf``'s CompatToolMapping is the single source of truth, read
  // by ``selector.select_proton_version``; the double-Proton problem is
  // handled at the umu spawn point by
  // ``launcher.proton.infrastructure.container_escape``.

  const onUpdate = play.update;
  const onPlay = play.play;
  const onUninstall = play.uninstall;
  const onResume = useCallback(() => {
    actions.launch(appId);
  }, [actions, appId]);
  const onStop = useCallback(() => {
    actions.terminate(appId);
  }, [actions, appId]);

  const primaryButtons = (() => {
    if (isRunning) {
      return (
        <>
          {/* Resume, not onPlay: the game is ALREADY running, so the
              pending-update prompt would be nonsense here — there is
              nothing left to decide, and updating over a running game
              is not something we want to offer. */}
          <DialogButton
            className={actionBtnClass("unifideck-resume-btn")}
            disabled={loading}
            onClick={onResume}
            style={actionBtnStyle}
          >
            <FaPlay /> {t("play.resume")}
          </DialogButton>
          <DialogButton
            className={iconBtnClass("unifideck-stop-btn")}
            onClick={onStop}
            style={iconBtnStyle}
            aria-label={t("play.stop")}
          >
            <FaTimes />
          </DialogButton>
        </>
      );
    }
    if (hasUpdate) {
      return (
        <DialogButton
          className={actionBtnClass("unifideck-update-btn")}
          disabled={loading || play.isWorking}
          onClick={onUpdate}
          style={actionBtnStyle}
        >
          <FaSyncAlt /> {t("play.update")}
        </DialogButton>
      );
    }
    return (
      <DialogButton
        className={actionBtnClass("unifideck-play-btn")}
        disabled={loading}
        onClick={onPlay}
        style={actionBtnStyle}
      >
        <FaPlay /> {t("play.play")}
      </DialogButton>
    );
  })();

  return (
    // autoFocus is intentional: claims gamepad focus for the primary action
    // eslint-disable-next-line jsx-a11y/no-autofocus
    <PlayShell autoFocus>
      {primaryButtons}
      <MetaInline
        sizeBytes={game?.size_bytes}
        showLastPlayed
        appId={appId}
        store={game?.store}
        gameId={game?.id}
        installed
      />
      <IconGroup>
        {game && (
          <CloudSaveButton
            store={game.store}
            gameId={game.id}
            gameTitle={game.title}
          />
        )}
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
        <DialogButton
          className={iconBtnClass()}
          style={iconBtnStyle}
          disabled={loading || play.isWorking}
          onClick={onUninstall}
          aria-label={t("play.uninstall")}
        >
          <FaTrash />
        </DialogButton>
      </IconGroup>
    </PlayShell>
  );
};
