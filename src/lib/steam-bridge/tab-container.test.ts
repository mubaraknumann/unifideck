// @vitest-environment jsdom
/**
 * Regression tests for the library-crash guard in the fake
 * collection's `GetAppCountWithToolsFilter` / `renderTabAddon`:
 * the 2026-07 Steam Beta reordered the tabs-useMemo deps and handed
 * the count path a collection object with no `Matches`, which threw
 * `appFilter.Matches is not a function` and error-boundaried the
 * entire Steam library. The count path must never throw, whatever
 * shape Steam passes.
 */
import { describe, it, expect, vi, afterEach } from "vitest";

// The Decky runtime is peer-provided in the Steam webview and absent
// under vitest — stub the imports that pull it in. (React resolves to
// src/test-support/react-stub.ts via the vitest alias.)
vi.mock("@decky/ui", () => ({ gamepadTabbedPageClasses: undefined }));
vi.mock("i18next", () => ({ default: { t: (key: string) => key } }));
vi.mock("../library-filters", () => ({
  runFilters: () => true,
  setStoreCountSink: () => {},
}));
// tab-container reads the device class to title the compat tab, and
// the real module pulls in @decky/api. Device-aware titling has its
// own tests in tab-title-device.test.ts; this file is about the
// count-path crash guard.
vi.mock("../device-type", () => ({
  getDeviceType: () => "deck",
  compatTabTitleKey: () => "deckTabs.deck",
}));

import { UnifideckTabContainer, tabManager, type SteamAppFilter } from "./tab-container";
import type { SteamAppOverview } from "../../types/steam";

function makeContainer(appids: number[] = []): UnifideckTabContainer {
  const container = new UnifideckTabContainer({
    id: "unifideck-test",
    title: "Test",
    position: 0,
    filters: [],
  });
  container.collection.visibleApps = appids.map((appid) => ({ appid } as SteamAppOverview));
  return container;
}

describe("GetAppCountWithToolsFilter guard", () => {
  it("applies a well-formed filter", () => {
    const c = makeContainer([1, 2, 3]);
    const filter: SteamAppFilter = { Matches: (a) => a.appid !== 2 };
    expect(c.collection.GetAppCountWithToolsFilter(filter)).toBe(2);
  });

  it("falls back to the unfiltered count when the filter is undefined", () => {
    const c = makeContainer([1, 2]);
    expect(c.collection.GetAppCountWithToolsFilter(undefined)).toBe(2);
  });

  it("falls back when handed an object without a callable Matches (Steam Beta deps shift)", () => {
    const c = makeContainer([1, 2, 3]);
    // What deps[6] became on the 2026-07 beta: a collection, not a filter.
    const notAFilter = {
      GetAppCountWithToolsFilter: () => 0,
    } as unknown as SteamAppFilter;
    expect(c.collection.GetAppCountWithToolsFilter(notAFilter)).toBe(3);
  });

  it("falls back when Matches itself throws", () => {
    const c = makeContainer([1, 2]);
    const throwing: SteamAppFilter = {
      Matches: () => {
        throw new Error("internal Steam change");
      },
    };
    expect(c.collection.GetAppCountWithToolsFilter(throwing)).toBe(2);
  });
});

describe("renderTabAddon", () => {
  it("renders a count without throwing when no filter was resolved", () => {
    const c = makeContainer();
    const tab = c.getActualTab((() => null) as never, null, {}, undefined);
    expect(tab).not.toBeNull();
    expect(tab?.renderTabAddon).toBeDefined();
    expect(() => tab?.renderTabAddon?.()).not.toThrow();
  });
});

describe("buildCollection type-games hydration race (UD-071)", () => {
  const win = window as unknown as { collectionStore?: unknown };

  afterEach(() => {
    delete win.collectionStore;
    vi.useRealTimers();
    vi.clearAllTimers();
  });

  it("schedules a retry (does not throw) when collectionStore is absent", () => {
    vi.useFakeTimers();
    delete win.collectionStore;
    const c = makeContainer();
    // No store yet: build leaves the collection empty and arms a retry.
    expect(() => c.buildCollection()).not.toThrow();
    expect(c.collection.allApps).toEqual([]);
    // A retry timer is pending — advancing time must not throw even
    // with the store still absent and the tab manager uninitialized.
    expect(() => vi.advanceTimersByTime(5000)).not.toThrow();
  });

  it("populates the collection once type-games hydrates", () => {
    win.collectionStore = {
      appTypeCollectionMap: new Map([
        ["type-games", { allApps: [{ appid: 42 } as SteamAppOverview] }],
      ]),
      GetCollection: () => null,
    };
    const c = makeContainer();
    c.buildCollection();
    expect(c.collection.allApps.map((a) => a.appid)).toEqual([42]);
  });
});

// Signing out never sweeps a store's shortcuts, so the game count alone
// kept the tab. Measured 2026-09-24: after an itch.io Disconnect and a
// Steam restart, the ITCH.IO 23 tab was still there.
describe("per-store tab visibility", () => {
  const itchVisible = () => tabManager.getTabs().some((t) => t.id === "unifideck-itch");

  afterEach(() => {
    tabManager.setSignedOutStores([]);
    tabManager.setStoreCounts({ itch: 0 });
  });

  it("hides a signed-out store's tab even when it still has games", () => {
    tabManager.initialize();
    tabManager.setStoreCounts({ itch: 23 });
    expect(itchVisible()).toBe(true);
    tabManager.setSignedOutStores(["itch"]);
    expect(itchVisible()).toBe(false);
  });

  it("shows it again once the store is signed back in", () => {
    tabManager.initialize();
    tabManager.setStoreCounts({ itch: 23 });
    tabManager.setSignedOutStores(["itch"]);
    tabManager.setSignedOutStores([]);
    expect(itchVisible()).toBe(true);
  });

  it("still hides a signed-in store with no games", () => {
    tabManager.initialize();
    expect(itchVisible()).toBe(false);
  });
});

// C.13 — flipping "Group duplicates" (or any other rebuildTabs trigger)
// used to leave every fresh UnifideckTabContainer's collection EMPTY
// until Steam happened to render that tab, so the count badge stayed
// stale until the user left and re-entered the library.
describe("rebuildTabs eagerly hydrates tab collections (C.13)", () => {
  const win = window as unknown as { collectionStore?: unknown };

  afterEach(() => {
    delete win.collectionStore;
  });

  it("populates visibleApps immediately, without a getActualTab() render", () => {
    win.collectionStore = {
      appTypeCollectionMap: new Map([
        ["type-games", { allApps: [{ appid: 7 } as SteamAppOverview] }],
      ]),
      GetCollection: () => null,
    };
    tabManager.initialize();
    tabManager.rebuildTabs();
    const allGames = tabManager.getTabs().find((t) => t.id === "unifideck-all");
    expect(allGames?.collection.visibleApps.map((a) => a.appid)).toEqual([7]);
  });
});
