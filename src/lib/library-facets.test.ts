// @vitest-environment jsdom
/**
 * Tests for the frontend library-facets module: loading the bulk
 * enrichment map and deriving shortcut-keyed Great-on-Deck compat
 * (the path that fixed non-Steam games missing from the Great-on-Deck
 * tab).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@decky/api", () => ({ call: vi.fn() }));
// useRPC pulls in React (hooks) which the jsdom transform can't load
// here; stub the one pure helper we use.
vi.mock("../api/useRPC", () => ({
  unwrapRpcEnvelope: (raw: unknown) =>
    raw && typeof raw === "object" && "success" in raw
      ? (raw as Record<string, unknown>).data
      : raw,
}));

import { call } from "@decky/api";
import {
  loadFacets,
  getFacet,
  getCompatByShortcutAppId,
  isFacetsLoaded,
  __resetFacetsForTest,
} from "./library-facets";

const mockCall = call as unknown as ReturnType<typeof vi.fn>;

const APPID = 2780953100;
const RECORD = {
  steam_app_id: 1147860,
  metacritic: 81,
  release_date: "21 Jun, 2022",
  recommendations_total: 41234,
  review_score: 8,
  review_percentage: 95,
  date_added_unix: 1690000000,
  compat_category: 3,
  store_category: [2, 1],
  store_tag: [1, 23],
  protondb_tier: "platinum",
  compat_status: "verified",
};

describe("library-facets", () => {
  beforeEach(() => {
    mockCall.mockReset();
    __resetFacetsForTest();
  });

  it("loads the enrichment map and resolves a facet by appid", async () => {
    mockCall.mockResolvedValue({ success: true, data: { [APPID]: RECORD } });
    await loadFacets(true);
    expect(isFacetsLoaded()).toBe(true);
    expect(getFacet(APPID)?.metacritic).toBe(81);
    expect(getFacet(APPID)?.store_category).toEqual([2, 1]);
  });

  it("derives Great-on-Deck compat by shortcut appid (no title matching)", async () => {
    mockCall.mockResolvedValue({ success: true, data: { [APPID]: RECORD } });
    await loadFacets(true);
    const compat = getCompatByShortcutAppId(APPID);
    expect(compat).not.toBeNull();
    expect(compat?.tier).toBe("platinum");
    expect(compat?.status).toBe("verified");
    expect(compat?.steamAppId).toBe(1147860);
  });

  it("returns null compat for an appid with no facet", async () => {
    mockCall.mockResolvedValue({ success: true, data: {} });
    await loadFacets(true);
    expect(getCompatByShortcutAppId(999999)).toBeNull();
  });

  it("degrades gracefully when the RPC throws", async () => {
    mockCall.mockRejectedValue(new Error("backend down"));
    await loadFacets(true);
    // No throw; just no data for the appid.
    expect(getCompatByShortcutAppId(APPID)).toBeNull();
  });

  it("keeps existing facets when a forced reload returns empty (sync race)", async () => {
    mockCall.mockResolvedValue({ success: true, data: { [APPID]: RECORD } });
    await loadFacets(true);
    expect(getFacet(APPID)).not.toBeNull();
    // Mid-sync the caches are briefly empty — a forced reload must NOT
    // wipe the good data we already hold.
    mockCall.mockResolvedValue({ success: true, data: {} });
    await loadFacets(true);
    expect(getFacet(APPID)).not.toBeNull();
  });
});
