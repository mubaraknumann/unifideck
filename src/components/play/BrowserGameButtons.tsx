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
 *
 * A merged Xbox PC row whose PC version cannot install (EA Play, Ubisoft+,
 * an unknown plan, a package choice) streams here too, with `notice`
 * naming why in one line under Stream.
 */
import { FC, useCallback } from "react";
import { DialogButton } from "@decky/ui";
import { useTranslation } from "react-i18next";
import {
  FaCloud,
  FaExclamationTriangle,
  FaGlobe,
  FaPlay,
  FaSignInAlt,
  FaTimes,
} from "react-icons/fa";
import { LunaIcon, SteamControllerIcon, SteamGearIcon } from "../shared";
import { useAppRunning } from "../../hooks/useAppRunning";
import { useGameActions } from "../../hooks/useGameActions";
import { useStoreAuth } from "../../hooks/useStoreAuth";
import { useToast } from "../../hooks/useToast";
import { SteamBridge } from "../../lib/steam-bridge";
import { headerText, toastText } from "../../services/toast-text";
import { usePlayActions } from "../../hooks/usePlayActions";
import type { StoreStatus } from "../../types/api";
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
import { PcReasonLine } from "./PcUnsupportedButtons";
import type { PcUnsupportedReason } from "../../lib/xbox-pc-errors";

interface Props {
  appId: number;
  variant: "stream" | "web";
  /** Where the game opens; used for the openurl fallback. Also the title's
   *  Browser_Game_Entry URL: an empty value for a gated store means the entry
   *  is missing, so Play must abort (Req 10.6). */
  url: string;
  /** Why the PC version of this title cannot install. */
  notice?: PcUnsupportedReason;
  /** Set for a connection-gated streaming store (Amazon Luna, Req 10). When
   *  present, the Play control reflects `authStatus`: enabled when connected,
   *  disabled with a sign-in prompt when not, a re-auth prompt when the
   *  session expired. Absent for xCloud / itch.io, which stay always-playable. */
  gatedStore?: "luna";
  /** The gated store's live connection status (from the shared auth-store). */
  authStatus?: StoreStatus;
}

const defaultBridge = new SteamBridge();

export const BrowserGameButtons: FC<Props> = ({
  appId,
  variant,
  url,
  notice,
  gatedStore,
  authStatus,
}) => {
  const { t } = useTranslation();
  const toast = useToast();
  const stream = variant === "stream";
  // While the Edge window is up the game is running: offer Resume (bring it
  // back to the front) and Stop, like an installed game, instead of a Play
  // button that would launch it a second time.
  const isRunning = useAppRunning(appId);
  const actions = useGameActions(defaultBridge);
  // Only the gated store (Luna) needs the connect flow; the hook is cheap and
  // reactive, so it is called unconditionally and only read when gated.
  const lunaAuth = useStoreAuth("luna");

  // Req 10: a Luna title is playable only while connected. Not-connected shows
  // a sign-in prompt + disabled Play; a session that expired shows a re-auth
  // prompt (distinct from first sign-in). xCloud / itch.io never gate.
  const isGated = gatedStore === "luna";
  const expired = authStatus === "expired";
  const connected = authStatus === "connected";
  const playable = !isGated || connected;

  const play = usePlayActions(appId, defaultBridge);
  const onPlay = useCallback(async () => {
    // A Luna Play press with no Browser_Game_Entry (empty URL) must abort the
    // launch, leave the connection state untouched, and show an error.
    if (isGated && !url) {
      toast.error(
        headerText("toasts.header.gameLaunch"),
        toastText("toasts.launcher.lunaEntryMissing"),
      );
      return;
    }
    await play.playBrowser(variant, url);
  }, [play, variant, url, isGated, toast]);

  // The sign-in / re-authentication action for a gated, not-yet-usable Luna
  // title: runs the same connect flow the QAM panel uses.
  const onSignIn = useCallback(() => {
    void lunaAuth.connect();
  }, [lunaAuth]);

  // Not connected (or session expired): render the sign-in / re-auth prompt
  // with a disabled Play, rather than a Play that would open a sign-in wall.
  if (isGated && !playable) {
    return (
      // autoFocus claims gamepad focus for the primary (sign-in) action.
      // eslint-disable-next-line jsx-a11y/no-autofocus
      <PlayShell autoFocus>
        <DialogButton
          className={actionBtnClass("unifideck-play-btn")}
          onClick={onSignIn}
          disabled={lunaAuth.busy}
          style={actionBtnStyle}
        >
          <FaSignInAlt />{" "}
          {expired ? t("play.luna.reauthenticate") : t("play.luna.signIn")}
        </DialogButton>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            fontSize: 12,
            color: expired ? "#f0b429" : "#b8bcbf",
          }}
        >
          {expired && <FaExclamationTriangle size={11} />}
          {expired ? t("play.luna.sessionExpired") : t("play.luna.signInHint")}
        </span>
        <MetaInline showLastPlayed appId={appId} />
      </PlayShell>
    );
  }

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
          {isGated ? <LunaIcon /> : stream ? <FaCloud /> : <FaGlobe />}{" "}
          {stream ? t("play.playOnCloud") : t("play.playInBrowser")}
        </DialogButton>
      )}

      {notice && <PcReasonLine reason={notice} />}
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
          onClick={(e) => openGearMenu(e?.currentTarget as HTMLElement, appId)}
          aria-label={t("playButton.appSettings")}
        >
          <SteamGearIcon />
        </DialogButton>
      </IconGroup>
    </PlayShell>
  );
};
