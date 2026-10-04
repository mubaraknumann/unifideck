/**
 * BrowserGameButtons: Play section for browser games.
 *
 * A browser game installs nothing and opens in an Edge kiosk window: an Xbox
 * Cloud Gaming stream (`variant="stream"`) or an itch.io HTML5 game
 * (`variant="web"`). Play goes through the same Steam `RunGame` path as an
 * installed game (`unifideck-launcher`, then the backend's
 * `launcher/browser_games` and `services/launcher/browser_game`), so Steam
 * Input and the Gaming Mode session work. If Steam's Apps surface is
 * unavailable we fall back to `steam://openurl` on the game's URL.
 *
 * This was `XCloudButtons` until the second kind arrived. The variant picks
 * the label, the icon and the default controller layout (a gamepad for a
 * stream, a trackpad mouse for a web page).
 */
import { FC, useCallback } from "react";
import { DialogButton } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaCloud, FaGlobe, FaPlay, FaTimes } from "react-icons/fa";
import { SteamControllerIcon, SteamGearIcon } from "../shared";
import { useAppRunning } from "../../hooks/useAppRunning";
import { useGameActions } from "../../hooks/useGameActions";
import { SteamBridge } from "../../lib/steam-bridge";
import { launchAppWithControllerLayout } from "../../utils/controllerConfig";
import { openNativeAppManageMenu } from "../../utils/nativeAppMenu";
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

interface Props {
  appId: number;
  variant: "stream" | "web";
  /** Where the game opens; used for the openurl fallback. */
  url: string;
}

const defaultBridge = new SteamBridge();

function openControllerConfig(appId: number): void {
  (
    window as unknown as {
      SteamClient?: {
        Apps?: { ShowControllerConfigurator?: (id: number) => void };
      };
    }
  ).SteamClient?.Apps?.ShowControllerConfigurator?.(appId);
}

function openAppSettings(appId: number): void {
  (
    window as unknown as {
      SteamClient?: {
        Apps?: { OpenAppSettingsDialog?: (id: number, page: string) => void };
      };
    }
  ).SteamClient?.Apps?.OpenAppSettingsDialog?.(appId, "general");
}

export const BrowserGameButtons: FC<Props> = ({ appId, variant, url }) => {
  const { t } = useTranslation();
  const stream = variant === "stream";
  // While the Edge window is up the game is running: offer Resume (bring it
  // back to the front) and Stop, like an installed game, instead of a Play
  // button that would launch it a second time.
  const isRunning = useAppRunning(appId);
  const actions = useGameActions(defaultBridge);

  const onPlay = useCallback(async () => {
    const launched = await launchAppWithControllerLayout(
      appId,
      stream ? "gamepad" : "web-browser",
    );
    if (!launched && url) {
      // Steam's Apps surface is unavailable: open the game directly.
      window.open(`steam://openurl/${url}`, "_blank");
    }
  }, [appId, stream, url]);

  return (
    // autoFocus is intentional: claims gamepad focus for the primary action
    // eslint-disable-next-line jsx-a11y/no-autofocus
    <PlayShell autoFocus>
      {isRunning ? (
        <>
          <DialogButton
            className={actionBtnClass("unifideck-resume-btn")}
            onClick={() => actions.launch(appId)}
            style={actionBtnStyle}
          >
            <FaPlay /> {t("play.resume")}
          </DialogButton>
          <DialogButton
            className={iconBtnClass("unifideck-stop-btn")}
            onClick={() => actions.terminate(appId)}
            style={iconBtnStyle}
            aria-label={t("play.stop")}
          >
            <FaTimes />
          </DialogButton>
        </>
      ) : (
        <DialogButton
          className={actionBtnClass("unifideck-play-btn")}
          onClick={onPlay}
          style={actionBtnStyle}
        >
          {stream ? <FaCloud /> : <FaGlobe />}{" "}
          {stream ? t("play.playOnCloud") : t("play.playInBrowser")}
        </DialogButton>
      )}

      <MetaInline showLastPlayed appId={appId} />

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
          onClick={(e) => {
            // Open Steam's native app menu (Manage / Properties / …),
            // matching the native gear; fall back to Properties directly.
            if (!openNativeAppManageMenu(e?.currentTarget as HTMLElement)) {
              openAppSettings(appId);
            }
          }}
          aria-label={t("playButton.appSettings")}
        >
          <SteamGearIcon />
        </DialogButton>
      </IconGroup>
    </PlayShell>
  );
};
