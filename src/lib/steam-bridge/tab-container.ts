/**
 * Tab definitions and per-tab collection container for the custom
 * Unifideck library tabs. Ported from staging:src/tabs/TabContainer.ts.
 *
 * `UNIFIDECK_TABS` declares the 10 tabs spliced into Steam's library.
 * `UnifideckTabContainer` wraps Steam's collection shape so each tab
 * is treated as a first-class collection (count, sort, filter).
 * `tabManager` is the singleton that the library-patch hook reads to
 * know which tabs to inject.
 */
import React, { ReactElement } from "react";
import { gamepadTabbedPageClasses } from "@decky/ui";
import i18n from "i18next";
import {
  runFilters,
  setStoreCountSink,
  type TabFilter,
} from "../library-filters";
import { compatTabTitleKey } from "../device-type";
import type { SteamAppOverview } from "../../types/steam";

const t = (key: string): string => i18n.t(key);

export interface UnifideckTab {
  id: string;
  title: string;
  position: number;
  filters: TabFilter[];
  icon?: string;
}

export function getUnifideckTabs(): UnifideckTab[] {
  return [
    {
      // Only the title varies by device. The id and its `deckCompat`
      // filter stay fixed, so nobody's tab layout moves when the label
      // changes.
      id: "unifideck-deck",
      title: t(compatTabTitleKey()),
      position: 0,
      filters: [{ type: "deckCompat", params: {} }],
    },
    {
      id: "unifideck-all",
      title: t("deckTabs.allGames"),
      position: 1,
      filters: [{ type: "all", params: {} }],
    },
    {
      id: "unifideck-installed",
      title: t("deckTabs.installed"),
      position: 2,
      filters: [{ type: "installed", params: { installed: true } }],
    },
    {
      id: "unifideck-steam",
      title: t("deckTabs.steam"),
      position: 3,
      filters: [{ type: "store", params: { store: "steam" } }],
    },
    {
      id: "unifideck-epic",
      title: t("deckTabs.epic"),
      position: 4,
      filters: [{ type: "store", params: { store: "epic" } }],
    },
    {
      id: "unifideck-gog",
      title: t("deckTabs.gog"),
      position: 5,
      filters: [{ type: "store", params: { store: "gog" } }],
    },
    {
      id: "unifideck-amazon",
      title: t("deckTabs.amazon"),
      position: 6,
      filters: [{ type: "store", params: { store: "amazon" } }],
    },
    {
      id: "unifideck-ubisoft",
      title: t("deckTabs.ubisoft"),
      position: 7,
      filters: [{ type: "store", params: { store: "ubisoft" } }],
    },
    {
      id: "unifideck-battlenet",
      title: t("deckTabs.battlenet"),
      position: 8,
      filters: [{ type: "store", params: { store: "battlenet" } }],
    },
    {
      id: "unifideck-microsoft",
      title: t("deckTabs.microsoft"),
      position: 9,
      filters: [{ type: "store", params: { store: "microsoft" } }],
    },
    {
      id: "unifideck-gamevault",
      title: t("deckTabs.gamevault"),
      position: 10,
      filters: [{ type: "store", params: { store: "gamevault" } }],
    },
    {
      // Stays last: "Non-Steam" is the catch-all, so a store tab inserted
      // before it takes its number and this one moves down.
      id: "unifideck-nonsteam",
      title: t("deckTabs.nonSteam"),
      position: 11,
      filters: [{ type: "nonSteam", params: {} }],
    },
  ];
}

/**
 * Steam's own tabs we replace with ours.
 *
 * Steam picks its compat tab's id from the device it is running on:
 * `GreatOnDeck` on a Deck, `GreatOnMachine` on a Steam Machine,
 * `SteamOSCompatible` on other SteamOS hardware (the Steam Frame reuses
 * the `GreatOnDeck` id). All three are listed unconditionally rather
 * than branching on `getDeviceType()` — an id Steam did not emit is
 * simply never matched, so the constant stays a constant and a Valve
 * build that changes which id it emits cannot strand a duplicate tab
 * next to ours.
 */
const DEFAULT_TABS_TO_HIDE = [
  "GreatOnDeck",
  "GreatOnMachine",
  "SteamOSCompatible",
  "AllGames",
  "Installed",
  "DesktopApps",
];

export function isTabMasterInstalled(): boolean {
  try {
    const plugins =
      (
        window as unknown as {
          DeckyPluginLoader?: { plugins?: Array<{ name?: string }> };
        }
      ).DeckyPluginLoader?.plugins ?? [];
    return plugins.some(
      (p) => p.name === "TabMaster" || p.name === "Tab Master",
    );
  } catch {
    return false;
  }
}

export function getHiddenDefaultTabs(): string[] {
  if (isTabMasterInstalled()) {
    console.log(
      "[Unifideck] TabMaster detected — keeping default tabs visible",
    );
    return [];
  }
  return DEFAULT_TABS_TO_HIDE;
}

export const HIDDEN_DEFAULT_TABS = DEFAULT_TABS_TO_HIDE;

/** Steam's collections app filter (the HW-compat/tools dropdown).
 *  Steam client builds reorder and reshape the internals this comes
 *  from, so callers must treat it as possibly missing and always
 *  feature-detect ``Matches`` before invoking it. */
export interface SteamAppFilter {
  Matches: (a: SteamAppOverview) => boolean;
}

interface SteamCollectionLike {
  AsDeletableCollection: () => null;
  AsDragDropCollection: () => null;
  AsEditableCollection: () => null;
  GetAppCountWithToolsFilter: (appFilter: SteamAppFilter | undefined) => number;
  bAllowsDragAndDrop: boolean;
  bIsDeletable: boolean;
  bIsDynamic: boolean;
  bIsEditable: boolean;
  displayName: string;
  id: string;
  allApps: SteamAppOverview[];
  visibleApps: SteamAppOverview[];
  apps: Map<number, SteamAppOverview>;
}

interface SteamGamesCollection {
  allApps?: SteamAppOverview[];
}

interface CollectionStoreLike {
  // ``GetCollection("type-games")`` is the documented path but
  // its inner MobX chain throws when the store is half-hydrated
  // at plugin-init time. ``appTypeCollectionMap`` is a raw
  // ``Map<string, Collection>`` that's populated earlier in the
  // hydration sequence and doesn't go through the failing
  // getter — same source TabMaster uses.
  GetCollection: (id: string) => SteamGamesCollection | null;
  appTypeCollectionMap?: Map<string, SteamGamesCollection>;
}

/** Steam tab shape consumed by the library-patch hook. */
export interface SteamTab {
  title: string;
  id: string;
  content: ReactElement;
  footer: Record<string, unknown>;
  renderTabAddon?: () => ReactElement;
}

// Bounded re-render retry for the collectionStore-not-hydrated race.
// When ``buildCollection`` runs before Steam's ``type-games``
// collection exists, every tab renders 0 and stays there until an
// unrelated re-render (UD-071). We ping the tab manager a few times
// with backoff so the strip re-renders — and re-runs
// ``buildCollection`` — once the store hydrates. Module-level (not
// per-container) because one hydration fixes every tab, so a single
// shared budget avoids N parallel timers.
const _HYDRATION_RETRY_MAX = 8;
const _HYDRATION_RETRY_BASE_MS = 400;
let hydrationRetryCount = 0;
let hydrationRetryTimer: ReturnType<typeof setTimeout> | null = null;

function scheduleHydrationRetry(): void {
  if (hydrationRetryTimer !== null) return; // one in flight already
  if (hydrationRetryCount >= _HYDRATION_RETRY_MAX) return;
  hydrationRetryCount += 1;
  const delay = _HYDRATION_RETRY_BASE_MS * hydrationRetryCount;
  hydrationRetryTimer = setTimeout(() => {
    hydrationRetryTimer = null;
    if (tabManager.isInitialized()) tabManager.rebuildTabs();
  }, delay);
}

function resetHydrationRetry(): void {
  hydrationRetryCount = 0;
  if (hydrationRetryTimer !== null) {
    clearTimeout(hydrationRetryTimer);
    hydrationRetryTimer = null;
  }
}

export class UnifideckTabContainer {
  id: string;
  title: string;
  position: number;
  filters: TabFilter[];
  collection: SteamCollectionLike;

  constructor(tab: UnifideckTab) {
    this.id = tab.id;
    this.title = tab.title;
    this.position = tab.position;
    this.filters = tab.filters;
    this.collection = this.makeEmptyCollection();
    // Don't call buildCollection here — at plugin-init time the
    // collectionStore is half-hydrated and its MobX-computed
    // getters throw. ``getActualTab`` calls ``buildCollection``
    // at render time when the store is fully ready, which is
    // when it actually matters.
  }

  private makeEmptyCollection(): SteamCollectionLike {
    return {
      AsDeletableCollection: () => null,
      AsDragDropCollection: () => null,
      AsEditableCollection: () => null,
      // A throw here propagates through Steam's tab renderer and
      // error-boundaries the ENTIRE library, so this must never
      // trust the filter's shape (Steam Beta 2026-07 moved it and
      // handed us a collection object instead). Unfiltered count is
      // the graceful fallback — visibleApps is already tab-filtered.
      GetAppCountWithToolsFilter: (appFilter) => {
        if (typeof appFilter?.Matches !== "function") {
          return this.collection.visibleApps.length;
        }
        try {
          return this.collection.visibleApps.filter((a) => appFilter.Matches(a))
            .length;
        } catch {
          return this.collection.visibleApps.length;
        }
      },
      bAllowsDragAndDrop: false,
      bIsDeletable: false,
      bIsDynamic: false,
      bIsEditable: false,
      displayName: this.title,
      id: this.id,
      allApps: [],
      visibleApps: [],
      apps: new Map(),
    };
  }

  buildCollection(): void {
    try {
      const cs = (
        window as unknown as { collectionStore?: CollectionStoreLike }
      ).collectionStore;
      // Prefer the raw appTypeCollectionMap (TabMaster's path) —
      // direct Map access doesn't go through MobX-computed
      // getters that may throw on half-hydrated stores. Fall back
      // to GetCollection if the raw map is unavailable.
      const all =
        cs?.appTypeCollectionMap?.get("type-games") ??
        cs?.GetCollection("type-games");
      if (!all) {
        // Steam's collectionStore isn't hydrated yet. Leaving the
        // collection empty here latches every tab to 0 until the
        // next unrelated re-render (UD-071 "works, then 0"). Schedule
        // a bounded re-render so tabs backfill once the store is ready.
        scheduleHydrationRetry();
        return;
      }
      // Store is hydrated — clear any pending hydration retry so a
      // later genuinely-empty library doesn't inherit stale budget.
      resetHydrationRetry();
      const filtered = (all.allApps ?? []).filter((app) =>
        runFilters(this.filters, app),
      );
      this.collection.allApps = filtered;
      this.collection.visibleApps = [...filtered];
      const map = new Map<number, SteamAppOverview>();
      for (const a of filtered) map.set(a.appid, a);
      this.collection.apps = map;
    } catch (e) {
      console.error("[Unifideck] buildCollection failed", e);
    }
  }

  getActualTab(
    TabAppGrid: React.ComponentType<Record<string, unknown>>,
    TabContext: React.Context<{ label: string }> | null,
    sortingProps: Record<string, unknown>,
    collectionAppFilter: SteamAppFilter | undefined,
    templateFooter?: Record<string, unknown>,
  ): SteamTab | null {
    this.buildCollection();
    const inner = React.createElement(TabAppGrid, {
      collection: this.collection,
      ...sortingProps,
    });
    const content = TabContext
      ? React.createElement(
          TabContext.Provider,
          { value: { label: this.title } },
          inner,
        )
      : inner;
    return {
      title: this.title,
      id: this.id,
      // Inherit the AllGames template's footer (keybinding hints,
      // menu callbacks). Steam's gamepad tab renderer expects
      // these fields populated; an empty ``{}`` makes tabs render
      // as no-op entries that are skipped in the nav strip.
      footer: { ...(templateFooter ?? {}) },
      content,
      // Steam invokes this on every tab-strip render; any throw
      // escapes into Steam's library error boundary, so the count
      // is computed defensively no matter what shape Steam hands us.
      renderTabAddon: () => {
        let count: number;
        try {
          count =
            this.collection.GetAppCountWithToolsFilter(collectionAppFilter);
        } catch {
          count = this.collection.visibleApps.length;
        }
        return React.createElement(
          "span",
          { className: gamepadTabbedPageClasses?.TabCount ?? "" },
          count,
        );
      },
    };
  }
}

type ConnectableStore =
  | "epic"
  | "gog"
  | "amazon"
  | "ubisoft"
  | "battlenet"
  | "microsoft"
  | "gamevault";

class TabManager {
  private tabs: UnifideckTabContainer[] = [];
  private initialized = false;
  private storeCounts: Record<ConnectableStore, number> = {
    epic: 0,
    gog: 0,
    amazon: 0,
    ubisoft: 0,
    battlenet: 0,
    microsoft: 0,
    gamevault: 0,
  };
  private version = 0;
  private listeners: (() => void)[] = [];

  getVersion(): number {
    return this.version;
  }

  onTabsChanged(listener: () => void): () => void {
    this.listeners.push(listener);
    return () => {
      this.listeners = this.listeners.filter((l) => l !== listener);
    };
  }

  private notifyListeners(): void {
    this.version++;
    this.listeners.forEach((l) => {
      try {
        l();
      } catch (e) {
        console.error("[TabManager] Listener failed:", e);
      }
    });
  }

  initialize(): void {
    if (this.initialized) return;
    this.tabs = getUnifideckTabs().map((tab) => new UnifideckTabContainer(tab));
    this.initialized = true;
  }

  getTabs(): UnifideckTabContainer[] {
    return this.tabs.filter((t) => this.shouldShowTab(t.id));
  }

  setStoreCounts(counts: Partial<Record<ConnectableStore, number>>): void {
    this.storeCounts = { ...this.storeCounts, ...counts };
  }

  // A per-store tab is shown only when that store has at least one
  // game. Connection/login state is deliberately ignored — an empty
  // store (even one the user is logged into) hides its tab until
  // games sync in, and reappears once they do.
  private shouldShowTab(id: string): boolean {
    const m: Record<string, ConnectableStore> = {
      "unifideck-epic": "epic",
      "unifideck-gog": "gog",
      "unifideck-amazon": "amazon",
      "unifideck-ubisoft": "ubisoft",
      "unifideck-battlenet": "battlenet",
      "unifideck-microsoft": "microsoft",
      "unifideck-gamevault": "gamevault",
    };
    const store = m[id];
    if (!store) return true;
    return this.storeCounts[store] > 0;
  }

  isInitialized(): boolean {
    return this.initialized;
  }

  rebuildTabs(): void {
    this.tabs = getUnifideckTabs().map((tab) => new UnifideckTabContainer(tab));
    this.notifyListeners();
  }
}

export const tabManager = new TabManager();

// Wire the cache loader → tab manager so per-store counts drive
// ``shouldShowTab`` automatically. The sink is module-level state
// on ``library-filters`` so a single registration covers every
// future ``loadUnifideckCache`` invocation.
setStoreCountSink((counts) => {
  tabManager.setStoreCounts(counts);
  if (tabManager.isInitialized()) tabManager.rebuildTabs();
});
