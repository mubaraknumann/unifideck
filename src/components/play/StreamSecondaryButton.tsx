/**
 * StreamSecondaryButton: the Stream button beside Install on a merged
 * Xbox PC + xCloud row that is not installed.
 *
 * Install stays the primary action. Stream goes through the same Steam
 * `RunGame` path `BrowserGameButtons` uses, with the gamepad layout: the
 * row has no `games.map` entry yet, so the launcher routes the launch to
 * the xCloud stream. There is no Stream on an installed row in v1, because
 * Play there runs the PC game.
 *
 * Hidden while the app is running: a second RunGame would only bring the
 * stream back to the front, which the Steam overlay already does.
 */
import { FC, useCallback } from "react";
import { DialogButton } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaCloud } from "react-icons/fa";
import { useAppRunning } from "../../hooks/useAppRunning";
import { launchAppWithControllerLayout } from "../../utils/controllerConfig";
import { iconBtnClass, iconBtnStyle } from "./PlayMeta";

interface Props {
  appId: number;
  /** The xCloud play URL; the fallback when Steam's Apps API is missing. */
  url: string;
}

export const StreamSecondaryButton: FC<Props> = ({ appId, url }) => {
  const { t } = useTranslation();
  const isRunning = useAppRunning(appId);

  const onStream = useCallback(async () => {
    const launched = await launchAppWithControllerLayout(appId, "gamepad");
    if (!launched && url) {
      // Steam's Apps surface is unavailable: open the stream directly.
      window.open(`steam://openurl/${url}`, "_blank");
    }
  }, [appId, url]);

  if (isRunning) return null;
  return (
    <DialogButton
      className={iconBtnClass()}
      style={iconBtnStyle}
      onClick={onStream}
      aria-label={t("play.playOnCloud")}
    >
      <FaCloud />
    </DialogButton>
  );
};
