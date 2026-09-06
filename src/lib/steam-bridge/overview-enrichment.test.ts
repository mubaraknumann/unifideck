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
import { loadFacets, __resetFacetsForTest } from "../library-facets";
import { enrichAllShortcuts } from "./overview-enrichment";

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
  compat_category: 3,
  compat_status: "verified",
  compat_categories: { deck: 3, machine: 2, steamos: 2 },
  store_category: [2, 1],
  store_tag: [1, 23],
  protondb_tier: "platinum",
};

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
    // Each device's rating lands in its own 2-bit slot: deck at 0,
    // steamos at 4, machine at 6. Writing only the Deck's is what made
    // our shortcuts invisible to a Steam Machine's native filters.
    // deck=3 | steamos=2<<4 | machine=2<<6 === 0b10_10_0011
    expect(ov.steam_hw_compat_category_packed).toBe(0b10_10_0011);
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
