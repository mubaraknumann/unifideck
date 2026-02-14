/**
 * Unifideck Library Patch
 *
 * Patches the Steam library to inject custom tabs that include
 * Epic, GOG, and Amazon games alongside Steam games.
 *
 * When TabMaster is detected, custom tabs are NOT injected - instead,
 * users can use [Unifideck] collections via TabMaster.
 */

import {
  afterPatch,
  findInReactTree,
  replacePatch,
  wrapReactType,
  Patch,
} from "@decky/ui";
import { RoutePatch, routerHook } from "@decky/api";
import { ReactElement, useEffect } from "react";
import {
  tabManager,
  getHiddenDefaultTabs,
  isTabMasterInstalled,
} from "./TabContainer";

// Cache for tab app grid component
let TabAppGridComponent: any = undefined;

/**
 * Adds a route patch, removing any existing patches first
 */
function addPatch(route: string, patch: RoutePatch): RoutePatch {
  // Remove any existing patches to prevent duplicates
  try {
    const existingPatches = [
      ...((
        window as any
      ).DeckyPluginLoader?.routerHook?.routerState?._routePatches?.get(route) ??
        []),
    ];
    existingPatches.forEach((existingPatch) => {
      if (patch.toString() === existingPatch.toString()) {
        routerHook.removePatch(route, existingPatch as RoutePatch);
      }
    });
  } catch (e) {
    // Ignore errors during cleanup
  }
  return routerHook.addPatch(route, patch);
}

/**
 * Patches the Steam library to show Unifideck tabs
 */
export function patchLibrary(): RoutePatch {
  // Initialize tab manager asynchronously
  tabManager
    .initialize()
    .catch((err) => console.error("[Unifideck] TabManager init error:", err));

  return addPatch(
    "/library",
    (props: { path: string; children: ReactElement }) => {
      // Check if TabMaster is installed
      if (isTabMasterInstalled()) {
        console.log(
          "[Unifideck] TabMaster detected - skipping custom tab injection (use [Unifideck] collections instead)",
        );
        // Don't inject tabs, let Steam + TabMaster handle it
        return props;
      }

      afterPatch(
        props.children,
        "type",
        (_: Record<string, unknown>[], ret1: ReactElement) => {
          if (!ret1?.type) {
            console.error("[Unifideck] Failed to find outer library element");
            return ret1;
          }

          let innerPatch: Patch;
          let memoCache: any;

          useEffect(() => {
            // Cleanup on unmount
            return () => {
              if (innerPatch) innerPatch.unpatch();
            };
          });

          // Patch the inner library component
          afterPatch(
            ret1,
            "type",
            (_: Record<string, unknown>[], ret2: ReactElement) => {
              if (!ret2?.type) {
                console.error(
                  "[Unifideck] Failed to find inner library element",
                );
                return ret2;
              }

              if (memoCache) {
                ret2.type = memoCache;
              } else {
                // @ts-ignore
                const origMemoComponent = ret2.type.type;
                // @ts-ignore
                wrapReactType(ret2);

                // Replace the component's type function
                innerPatch = replacePatch(ret2.type, "type", (args) => {
                  // Get React hooks from internal structure
                  const hooks =
                    (window.SP_REACT as any)
                      ?.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED
                      ?.ReactCurrentDispatcher?.current ||
                    Object.values(
                      (window.SP_REACT as any)
                        ?.__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE ||
                        {},
                    ).find((p: any) => p?.useEffect);

                  if (!hooks?.useMemo) {
                    return origMemoComponent(...args);
                  }

                  const realUseMemo = hooks.useMemo;

                  // Fake useMemo to intercept tab creation
                  const fakeUseMemo = (fn: () => any, deps: any[]) => {
                    return realUseMemo(() => {
                      const result = fn();

                      // Only intercept if we got an array
                      if (!Array.isArray(result)) {
                        return result;
                      }

                      // Steam's tab useMemo returns a tuple: [tabsArray, boolean]
                      // Detect this by checking if first element is an array of tab-like objects
                      let tabs: SteamTab[];
                      let isTuple = false;
                      let tupleRest: any[] = [];

                      if (
                        result.length >= 2 &&
                        Array.isArray(result[0]) &&
                        result[0].length > 0 &&
                        result[0][0]?.id &&
                        result[0][0]?.content
                      ) {
                        // Tuple format: [tabsArray, ...rest]
                        tabs = result[0];
                        isTuple = true;
                        tupleRest = result.slice(1);
                      } else if (
                        result.length > 0 &&
                        result[0]?.id &&
                        result[0]?.content
                      ) {
                        // Legacy format: direct tabs array
                        tabs = result;
                      } else {
                        // Not a tabs array, pass through
                        return result;
                      }

                      // Check if TabManager is initialized
                      if (!tabManager.isInitialized()) {
                        console.log(
                          "[Unifideck] TabManager not initialized, showing default tabs",
                        );
                        return result;
                      }

                      // Extract sorting props from deps
                      const [eSortBy, setSortBy, showSortingContextMenu] = deps;
                      const sortingProps = {
                        eSortBy,
                        setSortBy,
                        showSortingContextMenu,
                      };
                      const collectionsAppFilterGamepad = deps[6];

                      // Find a template tab to copy component structure from
                      const tabTemplate = tabs.find(
                        (tab: SteamTab) => tab?.id === "AllGames",
                      );
                      if (!tabTemplate) {
                        console.warn(
                          "[Unifideck] Could not find AllGames template tab in",
                          tabs.map((t: SteamTab) => t?.id),
                        );
                        return result;
                      }

                      // Find the TabAppGrid component
                      const TabAppGrid =
                        TabAppGridComponent ??
                        findInReactTree(tabTemplate.content, (elt: any) =>
                          elt?.type
                            ?.toString?.()
                            .includes("Library_FilteredByHeader"),
                        )?.type;

                      if (!TabAppGrid) {
                        console.warn(
                          "[Unifideck] Could not find TabAppGrid component",
                        );
                        return result;
                      }
                      TabAppGridComponent = TabAppGrid;

                      // Get TabContext for proper labeling
                      const TabContext = (tabTemplate.content.type as any)
                        ?._context;

                      // Build Unifideck tabs
                      const unifideckTabs = tabManager.getTabs();
                      const customTabs = unifideckTabs
                        .map((tabContainer) =>
                          tabContainer.getActualTab(
                            TabAppGrid,
                            TabContext,
                            sortingProps,
                            collectionsAppFilterGamepad,
                          ),
                        )
                        .filter((tab): tab is SteamTab => tab !== null);

                      // Filter out default tabs that we're replacing
                      const hiddenTabs = getHiddenDefaultTabs();
                      const filteredDefaultTabs = tabs.filter(
                        (tab) => !hiddenTabs.includes(tab.id),
                      );

                      const mergedTabs = [...customTabs, ...filteredDefaultTabs];

                      console.log(
                        `[Unifideck] Showing ${customTabs.length} custom tabs + ${filteredDefaultTabs.length} default tabs (hidden: ${hiddenTabs.length})`,
                      );

                      // Return in same format as received
                      if (isTuple) {
                        return [mergedTabs, ...tupleRest];
                      }
                      return mergedTabs;
                    }, deps);
                  };

                  // Temporarily replace useMemo
                  hooks.useMemo = fakeUseMemo;
                  const res = origMemoComponent(...args);
                  hooks.useMemo = realUseMemo;

                  return res;
                });

                memoCache = ret2.type;
              }

              return ret2;
            },
          );

          return ret1;
        },
      );

      return props;
    },
  );
}

// Type for Steam tabs
interface SteamTab {
  title: string;
  id: string;
  content: ReactElement;
  footer: any;
  renderTabAddon?: () => ReactElement;
}
