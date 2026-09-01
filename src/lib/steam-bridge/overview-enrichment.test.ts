// @vitest-environment jsdom
/**
 * Regression tests for the Home → "Recent Games" leak.
 *
 * Enrichment used to project each shortcut's `date_added_unix` into
 * `AppOverview.rt_purchased_time` so Steam's native "Date Added" sort
 * worked in our tabs. But Steam's Home shelf ranks by
 *   max(rt_last_time_locally_played, rt_purchased_time,
 *       installed ? rt_last_time_played_or_installed : 0)
 * over a pool that already contains every non-Steam shortcut, so a
 * freshly-synced (never-played) library floated straight to the top of
 * the shelf. `rt_purchased_time` must stay 0 for our shortcuts — while
 * every OTHER enrichment field still lands.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

// The Decky runtime is peer-provided in the Steam webview and absent
// under vitest — stub the imports that pull it in.
vi.mock("@decky/api", () => ({ call: vi.fn() }));
vi.mock("../../api/useRPC", () => ({
  unwrapRpcEnvelope: (raw: unknown) =>
    raw && typeof raw === "object" && "success" in raw
      ? (raw as Record<string, unknown>).data
      : raw,
}));
vi.mock("../library-filters", () => ({
  unifideckGameCache: new Map(),
}));

import { call } from "@decky/api";
import { unifideckGameCache } from "../library-filters";
import { loadFacets, __resetFacetsForTest } from "../library-facets";
import { enrichAllShortcuts, enrichInstalledState } from "./overview-enrichment";

const mockCall = call as unknown as ReturnType<typeof vi.fn>;

const NON_STEAM_APP_TYPE = 1073741824;
const APPID = 2780953100;
/** A shortcut Unifideck knows nothing about (user's own Firefox etc.). */
const FOREIGN_APPID = 3055621801;

const RELEASE = "21 Jun, 2022";
const RELEASE_UNIX = Math.floor(Date.parse(RELEASE) / 1000);

const RECORD = {
  steam_app_id: 1147860,
  metacritic: 81,
  release_date: RELEASE,
  recommendations_total: 41234,
  review_score: 8,
  review_percentage: 95,
  // The sync that first saw this game — the value that used to leak.
  date_added_unix: 1784412946,
  deck_category: 3,
  store_category: [2, 1],
  store_tag: [1, 23],
  protondb_tier: "platinum",
  deck_status: "verified",
};

interface TestPerClientEntry {
  clientid: string;
  client_name?: string;
  display_status?: number;
  status_percentage?: number;
  is_available_on_current_platform?: boolean;
  installed?: boolean;
}

interface TestOverview {
  appid: number;
  app_type: number;
  rt_purchased_time?: number;
  metacritic_score?: number;
  rt_original_release_date?: number;
  rt_steam_release_date?: number;
  review_score_with_bombs?: number;
  review_percentage_with_bombs?: number;
  steam_hw_compat_category_packed?: number;
  m_setStoreCategories?: Set<number>;
  m_setStoreTags?: Set<number>;
  per_client_data?: TestPerClientEntry[];
  most_available_clientid?: string;
}

function makeOverview(appid: number, purchasedTime: number): TestOverview {
  return {
    appid,
    app_type: NON_STEAM_APP_TYPE,
    rt_purchased_time: purchasedTime,
    m_setStoreCategories: new Set<number>(),
    m_setStoreTags: new Set<number>(),
  };
}

function installAppStore(overviews: TestOverview[]): void {
  const map = new Map<number, TestOverview>();
  for (const ov of overviews) map.set(ov.appid, ov);
  (window as unknown as { appStore: unknown }).appStore = { m_mapApps: map };
}

describe("overview enrichment — rt_purchased_time", () => {
  beforeEach(() => {
    mockCall.mockReset();
    __resetFacetsForTest();
    mockCall.mockResolvedValue({ success: true, data: { [APPID]: RECORD } });
  });

  it("never leaves a recency stamp on a synced shortcut", async () => {
    // A stamp left behind by an earlier build, still on the live
    // overview because the plugin reloaded without a Steam restart.
    const ov = makeOverview(APPID, RECORD.date_added_unix);
    installAppStore([ov]);

    await loadFacets(true);
    enrichAllShortcuts();

    expect(ov.rt_purchased_time).toBe(0);
  });

  it("still applies every other facet field", async () => {
    const ov = makeOverview(APPID, 0);
    installAppStore([ov]);

    await loadFacets(true);
    enrichAllShortcuts();

    expect(ov.metacritic_score).toBe(81);
    expect(ov.rt_original_release_date).toBe(RELEASE_UNIX);
    expect(ov.rt_steam_release_date).toBe(RELEASE_UNIX);
    expect(ov.review_score_with_bombs).toBe(8);
    expect(ov.review_percentage_with_bombs).toBe(95);
    // low 2 bits carry the Deck-compat category (3 = Verified)
    expect(ov.steam_hw_compat_category_packed).toBe(3);
    expect([...ov.m_setStoreCategories!]).toEqual([2, 1]);
    expect([...ov.m_setStoreTags!]).toEqual([1, 23]);
  });

  it("leaves shortcuts we don't own untouched", async () => {
    // No facet → not ours → we must not scrub the user's own data.
    const foreign = makeOverview(FOREIGN_APPID, 1747000000);
    installAppStore([foreign]);

    await loadFacets(true);
    enrichAllShortcuts();

    expect(foreign.rt_purchased_time).toBe(1747000000);
  });
});

describe("install-state enrichment", () => {
  beforeEach(() => {
    mockCall.mockReset();
    __resetFacetsForTest();
    unifideckGameCache.clear();
  });

  it("writes installed shape: display_status 11, status_percentage 100", () => {
    const ov = makeOverview(APPID, 0);
    installAppStore([ov]);

    enrichInstalledState(APPID, true);

    expect(ov.per_client_data).toHaveLength(1);
    expect(ov.per_client_data![0]).toMatchObject({
      clientid: "0",
      installed: true,
      display_status: 11,
      status_percentage: 100,
    });
  });

  it("writes not-installed shape: display_status 9, status_percentage absent", () => {
    const ov = makeOverview(APPID, 0);
    ov.per_client_data = [
      {
        clientid: "0",
        installed: true,
        display_status: 11,
        status_percentage: 100,
      },
    ];
    installAppStore([ov]);

    enrichInstalledState(APPID, false);

    expect(ov.per_client_data![0]).toMatchObject({
      clientid: "0",
      installed: false,
      display_status: 9,
    });
    expect("status_percentage" in ov.per_client_data![0]).toBe(false);
  });

  it("creates per_client_data array when absent", () => {
    const ov = makeOverview(APPID, 0);
    installAppStore([ov]);

    enrichInstalledState(APPID, true);

    expect(Array.isArray(ov.per_client_data)).toBe(true);
  });

  it("is a no-op for non-shortcut app types", () => {
    const ov = makeOverview(APPID, 0);
    ov.app_type = 1;
    installAppStore([ov]);

    enrichInstalledState(APPID, true);

    expect(ov.per_client_data).toBeUndefined();
  });

  it("is a no-op when overview is absent from appStore", () => {
    installAppStore([]);
    expect(() => enrichInstalledState(APPID, true)).not.toThrow();
  });

  it("enrichAllShortcuts writes install state from cache entry", async () => {
    mockCall.mockResolvedValue({ success: true, data: { [APPID]: RECORD } });
    await loadFacets(true);
    unifideckGameCache.set(APPID, { store: "epic", isInstalled: true });
    const ov = makeOverview(APPID, 0);
    installAppStore([ov]);

    enrichAllShortcuts();

    expect(ov.per_client_data![0]).toMatchObject({
      installed: true,
      display_status: 11,
      status_percentage: 100,
    });
  });

  it("enrichAllShortcuts skips install-state write when cache entry absent", async () => {
    mockCall.mockResolvedValue({ success: true, data: { [APPID]: RECORD } });
    await loadFacets(true);
    const ov = makeOverview(APPID, 0);
    installAppStore([ov]);

    enrichAllShortcuts();

    expect(ov.metacritic_score).toBe(81);
    expect(ov.per_client_data).toBeUndefined();
  });
});
