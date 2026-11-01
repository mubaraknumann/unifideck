/**
 * Library tab injection — patches Steam's `/library` route to splice
 * the custom Unifideck tabs into the tabbed page React tree.
 *
 * Ported from staging:src/tabs/LibraryPatch.ts. The technique:
 *
 *  1. Patch the `/library` route via `routerHook.addPatch`.
 *  2. `afterPatch` the outer + inner library elements until we reach
 *     the memoised component that owns the tabs array.
 *  3. Intercept its `useMemo` call to splice Unifideck tabs into the
 *     returned tab list before React commits.
 *
 * When TabMaster is installed we skip injection and rely on the
 * `[Unifideck]` Steam Collections (managed by CollectionManager).
 */
import {
  afterPatch,
  findInReactTree,
  replacePatch,
  wrapReactType,
  type Patch,
} from "@decky/ui";
import { ReactElement, useEffect, useState } from "react";
import {
  tabManager,
  getHiddenDefaultTabs,
  isTabMasterInstalled,
  type SteamAppFilter,
  type SteamTab,
} from "./tab-container";
import type { SteamBridge, RouterPatchHandle } from "./SteamBridge";

let cachedTabAppGrid: React.ComponentType<Record<string, unknown>> | undefined;

type LibraryRouteProps = { path: string; children: ReactElement };
type ReactComponentWithType = ReactElement & {
  type: { type?: unknown; _context?: React.Context<{ label: string }> | null };
};

interface ReactHooks {
  useMemo?: <T>(fn: () => T, deps: unknown[]) => T;
  useEffect?: unknown;
}

function getReactHooks(): ReactHooks | null {
  const reactInternals = (
    window as unknown as {
      SP_REACT?: {
        __SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED?: {
          ReactCurrentDispatcher?: { current?: ReactHooks };
        };
        __CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE?: Record<
          string,
          ReactHooks | undefined
        >;
      };
    }
  ).SP_REACT;
  const current =
    reactInternals?.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED
      ?.ReactCurrentDispatcher?.current;
  if (current?.useMemo) return current;
  const clientInternals =
    reactInternals?.__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE;
  if (clientInternals) {
    for (const candidate of Object.values(clientInternals)) {
      if (candidate?.useMemo && candidate.useEffect) return candidate;
    }
  }
  return null;
}

interface TabsTupleShape {
  isTuple: boolean;
  tabs: SteamTab[];
  rest: unknown[];
}

function detectTabsShape(result: unknown[]): TabsTupleShape | null {
  if (
    result.length >= 2 &&
    Array.isArray(result[0]) &&
    (result[0] as SteamTab[])[0]?.id &&
    (result[0] as SteamTab[])[0]?.content
  ) {
    return {
      isTuple: true,
      tabs: result[0] as SteamTab[],
      rest: result.slice(1),
    };
  }
  const first = result[0] as SteamTab | undefined;
  if (first?.id && first.content) {
    return { isTuple: false, tabs: result as SteamTab[], rest: [] };
  }
  return null;
}

// One-shot diagnostic logger — when something bails, log once
// per reason so the console isn't spammed every render.
const loggedBailReasons = new Set<string>();
function logBailOnce(reason: string, extra?: unknown): void {
  if (loggedBailReasons.has(reason)) return;
  loggedBailReasons.add(reason);
  console.warn(`[Unifideck Library] spliceTabs bailed: ${reason}`, extra ?? "");
}

function isAppFilter(v: unknown): v is SteamAppFilter {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as SteamAppFilter).Matches === "function"
  );
}

/** Locate Steam's collections app filter in the tabs useMemo deps.
 *  Matched by shape, never by position: Steam reorders this deps
 *  array across client builds (the 2026-07 beta inserted a dep,
 *  shifting the filter from index 6 to 7 and putting a collection —
 *  no ``Matches`` — where we used to read the filter). The filter is
 *  the only dep with a callable ``Matches``. */
function resolveAppFilter(deps: unknown[]): SteamAppFilter | undefined {
  const found = deps.find(isAppFilter);
  if (!found && !loggedBailReasons.has("app-filter-missing")) {
    loggedBailReasons.add("app-filter-missing");
    console.warn(
      "[Unifideck Library] collections app filter not found in useMemo deps;" +
        " tab counts fall back to unfiltered",
      deps.map((d) => typeof d),
    );
  }
  return found;
}

function spliceTabs(result: unknown, deps: unknown[]): unknown {
  if (!Array.isArray(result)) {
    logBailOnce("result is not an array", result);
    return result;
  }
  const shape = detectTabsShape(result);
  if (!shape) {
    logBailOnce("detectTabsShape returned null", result);
    return result;
  }
  // Idempotency — re-renders of the same route would otherwise
  // splice multiple times and duplicate our tabs.
  if (shape.tabs.some((t) => t.id?.startsWith("unifideck-"))) return result;
  if (!tabManager.isInitialized()) {
    logBailOnce("tabManager not initialized");
    return result;
  }

  const [eSortBy, setSortBy, showSortingContextMenu] = deps as [
    unknown,
    unknown,
    unknown,
  ];
  const sortingProps = { eSortBy, setSortBy, showSortingContextMenu };
  const collectionsAppFilterGamepad = resolveAppFilter(deps);

  const template = shape.tabs.find((t) => t.id === "AllGames");
  if (!template) {
    logBailOnce(
      "no AllGames template tab in",
      shape.tabs.map((t) => t.id),
    );
    return result;
  }

  const TabAppGrid =
    cachedTabAppGrid ??
    (
      findInReactTree(template.content, (elt) =>
        Boolean(elt?.type?.toString?.().includes("Library_FilteredByHeader")),
      ) as { type?: React.ComponentType<Record<string, unknown>> } | null
    )?.type;
  if (!TabAppGrid) {
    logBailOnce(
      "Library_FilteredByHeader component not found in AllGames template",
    );
    return result;
  }
  cachedTabAppGrid = TabAppGrid;

  const TabContext =
    (template.content as ReactComponentWithType).type?._context ?? null;

  const templateFooter = (template.footer ?? {}) as Record<string, unknown>;
  const customTabs = tabManager
    .getTabs()
    .map((c) =>
      c.getActualTab(
        TabAppGrid,
        TabContext,
        sortingProps,
        collectionsAppFilterGamepad,
        templateFooter,
      ),
    )
    .filter((t): t is SteamTab => t !== null);
  console.log(
    `[Unifideck Library] splicing ${customTabs.length} custom tabs into Steam library`,
  );

  const hidden = getHiddenDefaultTabs();
  const filteredDefaults = shape.tabs.filter((t) => !hidden.includes(t.id));
  const merged = [...customTabs, ...filteredDefaults];

  if (shape.isTuple) return [merged, ...shape.rest];
  return merged;
}

/** Patches the `/library` route to inject Unifideck tabs.
 *  Same pattern as TabMaster — proven to work in both Desktop
 *  and Gaming Mode. */
export function applyLibraryPatch(bridge: SteamBridge): RouterPatchHandle {
  tabManager.initialize();
  return bridge.addRouterPatch("/library", (rawProps: unknown) => {
    const props = rawProps as LibraryRouteProps;

    // Hooks before the TabMaster check, never after: that check can flip
    // while the library is mounted (TabMaster installed, removed or reloaded,
    // or loaded after us), and a render calling fewer hooks than the last
    // makes React throw and takes the whole library route down.
    const [, setVersion] = useState(0);
    useEffect(() => {
      return tabManager.onTabsChanged(() => {
        setVersion((v) => v + 1);
      });
    }, []);
    if (isTabMasterInstalled()) return props;

    afterPatch(
      props.children as never,
      "type",
      (_: unknown[], ret1: ReactElement) => {
        if (!ret1?.type) return ret1;
        let innerPatch: Patch | undefined;
        let memoCache: unknown;

        useEffect(() => () => {
          innerPatch?.unpatch();
        });

        afterPatch(
          ret1 as never,
          "type",
          (_2: unknown[], ret2: ReactElement) => {
            if (!ret2?.type) return ret2;
            const ret2t = ret2 as ReactComponentWithType;
            if (memoCache) {
              ret2t.type = memoCache as ReactComponentWithType["type"];
              return ret2;
            }
            const origMemoComponent = ret2t.type.type as (
              ...args: unknown[]
            ) => unknown;
            // Newer Steam builds can leave `.type.type` null/undefined on
            // some render paths; wrapping it would make the replaced fn call
            // `undefined(...args)` and crash the whole library tab. Bail to
            // the untouched tree if there's no inner component to wrap.
            if (typeof origMemoComponent !== "function") return ret2;
            wrapReactType(ret2 as never);
            innerPatch = replacePatch(
              ret2t.type as never,
              "type",
              (args: unknown[]) => {
                const hooks = getReactHooks();
                if (!hooks?.useMemo) return origMemoComponent(...args);
                const realUseMemo = hooks.useMemo;
                hooks.useMemo = <T>(fn: () => T, deps: unknown[]): T => {
                  const enrichedDeps = [...deps, tabManager.getVersion()];
                  return realUseMemo(
                    () => spliceTabs(fn(), enrichedDeps) as T,
                    enrichedDeps,
                  );
                };
                try {
                  return origMemoComponent(...args);
                } finally {
                  hooks.useMemo = realUseMemo;
                }
              },
            );
            memoCache = ret2t.type;
            return ret2;
          },
        );
        return ret1;
      },
    );
    return props;
  });
}
