/**
 * "Play from": Steam's own arrow on the action button, offering the cloud
 * for a game that can also be streamed.
 *
 * Steam shows this arrow when a game can be played in more than one place
 * (installed here, or streamed from another PC). We use the same seam: the
 * action button's overview offers a second client (`withPlayTargets`), and
 * the arrow opens our menu instead of Steam's device picker
 * (`bindStreamMenu`). Choosing the cloud turns Steam's button into its own
 * green "Stream / from Xbox Cloud"; the choice is remembered per game.
 *
 * It replaces the separate cloud button beside Install on the game page
 * (`StreamSecondaryButton`, still used by the legacy panel). It offers the
 * cloud on every merged Xbox PC + xCloud row: one that is not installed
 * streams as that button did; an installed one streams through a one-run
 * launch option ({@link CLOUD_LAUNCH_TOKEN}), since its plain Play runs the
 * PC game.
 */
import { Menu, MenuItem, showContextMenu } from "@decky/ui";
import { useCallback, useState } from "react";
import { steamText } from "../../lib/steam-bridge/native-app-details";
import { launchOnceWithToken } from "../../lib/steam-bridge/one-run-launch";
import { ensureGamepadConfigForApp } from "../../utils/controllerConfig";
import { findClassMap } from "../../lib/steam-bridge/native-app-details/webpack";
import type { PlaySectionState } from "../../hooks/resolvePlaySection";

export type PlayTarget = "local" | "cloud";

/** The cloud's name in Steam's "Stream from %1$s". A brand name, the same
 *  in every language. */
export const CLOUD_TARGET_NAME = "Xbox Cloud";

const storageKey = (appId: number): string => `unifideck.playTarget.${appId}`;

/** The stream URL when this row offers the cloud, else null: a merged
 *  Xbox PC + xCloud row, installed or not. */
export function cloudStreamUrl(state: PlaySectionState): string | null {
  return (state.kind === "not-installed" || state.kind === "installed") &&
    state.streamUrl
    ? state.streamUrl
    : null;
}

/** The one-run launch option that streams an installed game this time
 *  (read by the launcher's Xbox routing). */
export const CLOUD_LAUNCH_TOKEN = "UNIFIDECK_PLAY_TARGET=cloud";

/**
 * Stream an installed game this once. Its plain Play runs the PC game, so
 * the launch carries {@link CLOUD_LAUNCH_TOKEN}; the gamepad layout and the
 * fallback are the ones a not-installed row's stream uses.
 */
export async function streamInstalled(
  appId: number,
  url: string,
): Promise<void> {
  if (await launchOnceWithToken(appId, CLOUD_LAUNCH_TOKEN)) {
    void ensureGamepadConfigForApp(appId);
    return;
  }
  // Steam's Apps surface is unavailable: open the stream directly.
  window.open(`steam://openurl/${url}`, "_blank");
}

/** The remembered place to play this game from. */
export function usePlayTarget(
  appId: number,
): [PlayTarget, (t: PlayTarget) => void] {
  const [target, setState] = useState<PlayTarget>(() => readTarget(appId));
  const set = useCallback(
    (t: PlayTarget) => {
      try {
        localStorage.setItem(storageKey(appId), t);
      } catch {
        /* storage unavailable: the choice lasts this page only */
      }
      setState(t);
    },
    [appId],
  );
  return [target, set];
}

function readTarget(appId: number): PlayTarget {
  try {
    return localStorage.getItem(storageKey(appId)) === "cloud"
      ? "cloud"
      : "local";
  } catch {
    return "local";
  }
}

/** Open the "Play from" menu at `anchor`, Steam's own context menu. */
export function showPlayFromMenu(
  anchor: EventTarget,
  target: PlayTarget,
  onSelect: (t: PlayTarget) => void,
): void {
  const mark = (t: PlayTarget): string => (target === t ? "✓  " : "");
  showContextMenu(
    <Menu label={steamText("#GameAction_PlayFrom", "Play from")}>
      <MenuItem onSelected={() => onSelect("local")}>
        {mark("local") +
          steamText("#StreamingClient_Select_ThisSteamDeck", "This Steam Deck")}
      </MenuItem>
      <MenuItem onSelected={() => onSelect("cloud")}>
        {mark("cloud") + CLOUD_TARGET_NAME}
      </MenuItem>
    </Menu>,
    anchor,
  );
}

/** Class added to our split buttons, to scope {@link remotePlayTipCss}. */
export const PLAY_FROM_CLASS = "unifideck-play-from";

let tipCss: string | null = null;

/**
 * CSS hiding Steam's Remote Play tip bubble on our arrow. Steam bobs it on
 * the arrow of a game with several clients, saying "stream from your PC",
 * which is not what ours does.
 */
export function remotePlayTipCss(): string {
  if (tipCss != null) return tipCss;
  const c = findClassMap([
    "StreamingSelector",
    "StreamingCallout",
    "StreamingCalloutMessageContainer",
  ]);
  tipCss = [c.StreamingCallout, c.StreamingCalloutMessageContainer]
    .filter(Boolean)
    .map((k) => `.${PLAY_FROM_CLASS} .${k}{display:none!important}`)
    .join("");
  return tipCss;
}
