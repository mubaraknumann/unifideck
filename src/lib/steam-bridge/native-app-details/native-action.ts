/**
 * Our play-section state → what Steam's own action button shows and does.
 *
 * Pure, so every row of the table is unit-tested (`native-action.test.ts`).
 *
 * | Our state                       | Native button          | Click            |
 * |---------------------------------|------------------------|------------------|
 * | Steam is launching / running    | Steam's own (Cancel, Resume + ✕) | Steam's own |
 * | not ours (`missing`)            | Steam's own            | Steam's own      |
 * | loading / backend unavailable   | Steam's own label      | Play → Steam's own RunGame; anything else does nothing |
 * | not installed                   | Install                | our install flow |
 * | downloading                     | Cancel                 | our cancel prompt |
 * | installed, update pending       | Update                 | our update       |
 * | installed                       | Play (green)           | our play         |
 * | browser game                    | Play (green)           | our browser play |
 * | Xbox PC, cannot install / run   | disabled Install / Play | nothing         |
 *
 * "Does nothing" matters: Steam's own Install would open Steam's install
 * wizard for a shortcut app id.
 */
import { DisplayStatus, LIVE_DISPLAY_STATUSES } from "./overview-proxy";
import type { PlaySectionState } from "../../../hooks/resolvePlaySection";
import type { DownloadItem } from "../../../types/downloads";

export type NativeIntent =
  | { kind: "native" }
  | { kind: "none" }
  | { kind: "install" }
  | { kind: "cancel"; download: DownloadItem }
  | { kind: "update" }
  | { kind: "play" }
  | { kind: "browser"; variant: "stream" | "web"; url: string };

export interface NativeAction {
  /** Status the button is shown with, or `null` to show the real overview. */
  status: number | null;
  intent: NativeIntent;
}

const NATIVE: NativeAction = { status: null, intent: { kind: "native" } };

export function resolveNativeAction(
  state: PlaySectionState,
  infoStatus: string | undefined,
  realStatus: number | undefined,
  hasUpdate: boolean,
): NativeAction {
  if (realStatus != null && LIVE_DISPLAY_STATUSES.has(realStatus)) {
    return NATIVE;
  }
  switch (state.kind) {
    case "steam-native":
    case "unavailable":
      if (infoStatus === "missing") return NATIVE;
      // Our answer is not in yet. Play is safe to leave to Steam (it is
      // the same RunGame we would call); Steam's Install is not.
      return realStatus === DisplayStatus.Play
        ? NATIVE
        : { status: realStatus ?? null, intent: { kind: "none" } };
    case "not-installed":
      return { status: DisplayStatus.Install, intent: { kind: "install" } };
    case "downloading":
      return {
        status: DisplayStatus.Cancel,
        intent: { kind: "cancel", download: state.download },
      };
    case "installed":
      return hasUpdate
        ? { status: DisplayStatus.Update, intent: { kind: "update" } }
        : { status: DisplayStatus.Play, intent: { kind: "play" } };
    case "browser":
      return {
        status: DisplayStatus.Play,
        intent: { kind: "browser", variant: state.variant, url: state.url },
      };
    case "pc-unsupported":
      return {
        status: state.installed
          ? DisplayStatus.PlayUnavailable
          : DisplayStatus.InstallUnavailable,
        intent: { kind: "none" },
      };
    default:
      return NATIVE;
  }
}
