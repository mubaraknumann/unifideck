/**
 * GameDetailsPatching.tsx
 *
 * Steam UI patching logic for injecting Unifideck components into game details pages.
 * Uses Decky's routerHook to patch the /library/app/:appid route.
 *
 * ARCHITECTURE:
 * - Uses routerHook.addPatch to intercept React route rendering in Steam's process
 * - React.createElement creates components in Steam's React tree
 * - Steam's reconciler renders these in its own DOM
 * - This is the ONLY way to inject UI into Steam's game details page (CEF process isolation)
 */

import React from "react";
import { routerHook } from "@decky/api";
import {
  afterPatch,
  findInReactTree,
  createReactTreePatcher,
  appDetailsClasses,
  appActionButtonClasses,
  playSectionClasses,
  appDetailsHeaderClasses,
} from "@decky/ui";

/**
 * Patch the game details route to inject custom components.
 *
 * @param InstallInfoDisplay - Component to show install/download info
 * @param GameInfoPanel - Component to show metadata for non-Steam games
 * @returns Unpatch function
 */
export function patchGameDetailsRoute(
  InstallInfoDisplay: React.ComponentType<{ appId: number }>,
  GameInfoPanel: React.ComponentType<{ appId: number }>,
) {
  return routerHook.addPatch("/library/app/:appid", (routerTree: any) => {
    const routeProps = findInReactTree(routerTree, (x: any) => x?.renderFunc);
    if (!routeProps) return routerTree;

    // Create tree patcher (SAFE: mutates BEFORE React reconciles)
    const patchHandler = createReactTreePatcher(
      [
        // Finder function: return children array (NOT overview object) - ProtonDB pattern
        (tree) =>
          findInReactTree(tree, (x: any) => x?.props?.children?.props?.overview)
            ?.props?.children,
      ],
      (_, ret) => {
        // Patcher function: SAFE to mutate here (before reconciliation)
        // Extract appId from ret (not from finder closure)
        const overview = findInReactTree(
          ret,
          (x: any) => x?.props?.children?.props?.overview,
        )?.props?.children?.props?.overview;

        if (!overview) return ret;
        const appId = overview.appid;

        // DISABLED: Store patching disabled, so no metadata injection
        // TODO: Re-enable once we figure out what's breaking Steam
        // const isShortcut = appId > 2000000000;
        // if (isShortcut) {
        //   const signedAppId = appId > 0x7FFFFFFF ? appId - 0x100000000 : appId;
        //   console.log(`[Unifideck] Game details opened for shortcut: ${appId} (signed: ${signedAppId})`);
        //   injectGameToAppinfo(signedAppId);
        // }

        try {
          // Strategy: Find the Header area (contains Play button and game info)
          // The Header is at the top of the game details page, above the scrollable content

          // Look for the AppDetailsHeader container first (best position)
          const headerContainer = findInReactTree(
            ret,
            (x: any) =>
              Array.isArray(x?.props?.children) &&
              (x?.props?.className?.includes(appDetailsClasses?.Header) ||
                x?.props?.className?.includes(
                  appDetailsHeaderClasses?.TopCapsule,
                )),
          );

          // Find the PlaySection container (where Play button lives)
          const playSection = findInReactTree(
            ret,
            (x: any) =>
              Array.isArray(x?.props?.children) &&
              x?.props?.className?.includes(playSectionClasses?.Container),
          );

          // Alternative: Find the AppButtonsContainer
          const buttonsContainer = findInReactTree(
            ret,
            (x: any) =>
              Array.isArray(x?.props?.children) &&
              x?.props?.className?.includes(
                playSectionClasses?.AppButtonsContainer,
              ),
          );

          // Find the game info row (typically contains play button, shortcuts, settings)
          const gameInfoRow = findInReactTree(
            ret,
            (x: any) =>
              Array.isArray(x?.props?.children) &&
              x?.props?.style?.display === "flex" &&
              x?.props?.children?.some?.(
                (c: any) =>
                  c?.props?.className?.includes?.(
                    appActionButtonClasses?.PlayButtonContainer,
                  ) || c?.type?.toString?.()?.includes?.("PlayButton"),
              ),
          );

          // Find InnerContainer as fallback (original approach)
          const innerContainer = findInReactTree(
            ret,
            (x: any) =>
              Array.isArray(x?.props?.children) &&
              x?.props?.className?.includes(appDetailsClasses?.InnerContainer),
          );

          // ProtonDB COMPATIBILITY: Always use InnerContainer first to match ProtonDB's behavior
          // When multiple plugins modify the SAME container, patches chain correctly.
          // When plugins modify DIFFERENT containers (parent vs child), React reconciliation conflicts occur.
          // Since InstallInfoDisplay uses position: absolute, it works in any container.
          let container =
            innerContainer ||
            headerContainer ||
            playSection ||
            buttonsContainer ||
            gameInfoRow;

          // If none of those work, log but try to proceed with whatever we have (or return)
          if (!container) {
            console.log(
              `[Unifideck] No suitable container found for app ${appId}, skipping injection`,
            );
            return ret;
          }

          // Ensure children is an array
          if (!Array.isArray(container.props.children)) {
            container.props.children = [container.props.children];
          }

          // ProtonDB COMPATIBILITY: Insert at index 2
          // ProtonDB inserts at index 1. By inserting at index 2, we:
          // 1. Avoid overwriting ProtonDB's element
          // 2. Stay early in the children array so focus navigation works
          // Since InstallInfoDisplay uses position: absolute, its visual position is CSS-controlled.
          const spliceIndex = Math.min(2, container.props.children.length);

          // Inject our install info display after play button
          container.props.children.splice(
            spliceIndex,
            0,
            React.createElement(InstallInfoDisplay, {
              key: `unifideck-install-info-${appId}`,
              appId,
            }),
          );

          console.log(
            `[Unifideck] Injected install info for app ${appId} in ${
              innerContainer
                ? "InnerContainer"
                : headerContainer
                ? "Header"
                : playSection
                ? "PlaySection"
                : buttonsContainer
                ? "ButtonsContainer"
                : "GameInfoRow"
            } at index ${spliceIndex}`,
          );

          // ========== GAME INFO PANEL INJECTION ==========
          // For non-Steam games, inject our custom GameInfoPanel to display metadata
          // Non-Steam shortcuts have appId > 2000000000
          const isNonSteamGame = appId > 2000000000;
          console.log(
            `[Unifideck] Checking GameInfoPanel injection: appId=${appId}, isNonSteamGame=${isNonSteamGame}`,
          );
          if (isNonSteamGame) {
            try {
              // Add GameInfoPanel to the same container, after InstallInfoDisplay
              container.props.children.splice(
                spliceIndex + 1, // Insert after InstallInfoDisplay
                0,
                React.createElement(GameInfoPanel, {
                  key: `unifideck-game-info-${appId}`,
                  appId,
                }),
              );
              console.log(
                `[Unifideck] Injected GameInfoPanel for non-Steam game ${appId}`,
              );
            } catch (panelError) {
              console.error(
                `[Unifideck] Error creating GameInfoPanel:`,
                panelError,
              );
            }
          }
        } catch (error) {
          console.error("[Unifideck] Error injecting install info:", error);
        }

        return ret; // Always return modified tree
      },
    );

    // Apply patcher to renderFunc
    afterPatch(routeProps, "renderFunc", patchHandler);

    return routerTree;
  });
}
