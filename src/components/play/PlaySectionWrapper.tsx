/**
 * PlaySectionWrapper — dispatcher for the Play section.
 *
 * Reads `usePlaySection(appId)` and renders one of the
 * sub-components based on the discriminated state. While the
 * game info is still resolving, a `PlayLoadingSkeleton` keeps the
 * row from collapsing.
 *
 * It also renders the scoped CSS rules ({@link nativeAppDetailsHideCss})
 * that hide Steam's native Play row and the lower tabbed section
 * (Activity / Community / Game Info). Those live in a *separate*
 * subtree (so they can't be removed in-tree); the `AppDetailsPatch`
 * marks a common ancestor and these rules hide the targets beneath
 * it — synchronous, no RPC/CDP round-trip. The rules render
 * unconditionally so the native UI never paints on a shortcut page,
 * even before `get_game_info` resolves.
 *
 * The wrapper itself is pure presentation : the decision logic is
 * in `usePlaySection`, the actions are in `useGameActions`. This
 * component just glues them.
 */
import { FC, ReactNode, useEffect } from "react";
import { usePlaySection } from "../../hooks/usePlaySection";
import { NotInstalledButtons } from "./NotInstalledButtons";
import { DownloadingButtons } from "./DownloadingButtons";
import { InstalledButtons } from "./InstalledButtons";
import { BrowserGameButtons } from "./BrowserGameButtons";
import { PlayLoadingSkeleton } from "./PlayMeta";
import {
  injectPlayFocusStyles,
  nativeAppDetailsHideCss,
  HLTB_CLEAN_BOX_POSITION_CSS,
} from "./play.css";

/**
 * Props of {@link PlaySectionWrapper}. The `appId` is the Steam
 * shortcut id ; everything else is resolved through hooks inside
 * the wrapper.
 */
export interface PlaySectionWrapperProps {
  appId: number;
}

/**
 * Top-level wrapper rendered in place of Steam's own Play section
 * for Unifideck games. Picks the right variant (Not-installed /
 * Downloading / Installed / browser game) based on `usePlaySection`, or a
 * loading skeleton while the game info resolves.
 */
export const PlaySectionWrapper: FC<PlaySectionWrapperProps> = ({ appId }) => {
  const state = usePlaySection(appId);
  useEffect(() => {
    injectPlayFocusStyles();
  }, []);

  let body: ReactNode;
  if (!state.shouldOverride) {
    // Info still resolving for this shortcut (its store is never
    // "steam", so this is the only reason shouldOverride is false).
    body = <PlayLoadingSkeleton />;
  } else {
    switch (state.kind) {
      case "not-installed":
        body = <NotInstalledButtons appId={appId} />;
        break;
      case "downloading":
        body = <DownloadingButtons download={state.download} />;
        break;
      case "installed":
        body = <InstalledButtons appId={state.appId} />;
        break;
      case "browser":
        body = (
          <BrowserGameButtons
            appId={state.appId}
            variant={state.variant}
            url={state.url}
          />
        );
        break;
      default:
        body = <PlayLoadingSkeleton />;
    }
  }

  return (
    <>
      <style>{nativeAppDetailsHideCss() + HLTB_CLEAN_BOX_POSITION_CSS}</style>
      {body}
    </>
  );
};
