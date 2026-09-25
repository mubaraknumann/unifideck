import { describe, expect, it } from "vitest";
import { groupGames, storePriorityRank, STORE_PRIORITY } from "./game-grouping";
import type { Game, StoreId } from "../types/api";

// Every `StoreId` member, kept in sync by hand with `types/api.ts` —
// there's no runtime array to derive this from (it's a type), so this
// list is the regression guard for D.16: a `StoreId` added without a
// matching `STORE_PRIORITY` entry used to sort FIRST, not last (see
// `storePriorityRank`'s docstring).
const ALL_STORE_IDS: StoreId[] = [
  "steam",
  "epic",
  "gog",
  "amazon",
  "microsoft",
  "ubisoft",
  "battlenet",
  "gamevault",
  "itch",
];

let nextAppId = 1;

/** Builds a fake wire-shaped `Game` — deliberately WITHOUT `id`, since
 *  the real `get_all_unifideck_games` RPC never sends one (see
 *  `gameKey`'s docstring). A regression test below pins that grouping
 *  must still work correctly from this exact shape. */
function g(overrides: Partial<Game> & { store: Game["store"] }): Game {
  const appId = overrides.app_id ?? nextAppId++;
  return {
    store_game_id: `game-${appId}`,
    title: `game-${appId}`,
    is_installed: false,
    app_id: appId,
    ...overrides,
  } as Game;
}

describe("groupGames", () => {
  it("keeps ungrouped games as their own singleton group", () => {
    const games = [g({ store: "epic" }), g({ store: "gog" })];
    const groups = groupGames(games);
    expect(groups).toHaveLength(2);
    expect(groups.map((grp) => grp.games.length)).toEqual([1, 1]);
  });

  it("never merges unrelated ungrouped games — the id-fallback regression", () => {
    // Real wire rows carry no `id` field at all (only app_id/store/
    // store_game_id). A fallback key that read `game.id` evaluated to
    // `undefined` for every one of these, merging all of them into a
    // single bucket instead of N singleton groups.
    const games = Array.from({ length: 5 }, () => g({ store: "epic" }));
    const groups = groupGames(games);
    expect(groups).toHaveLength(5);
  });

  it("merges games sharing a dedupe_group_id into one group", () => {
    const games = [
      g({ store: "epic", dedupe_group_id: "behind-the-frame" }),
      g({ store: "amazon", dedupe_group_id: "behind-the-frame" }),
    ];
    const groups = groupGames(games);
    expect(groups).toHaveLength(1);
    expect(groups[0].games).toHaveLength(2);
  });

  it("prefers an installed copy as primary over store priority", () => {
    const games = [
      g({ store: "epic", dedupe_group_id: "x" }),
      g({
        store: "gog",
        dedupe_group_id: "x",
        installed: true,
        is_installed: true,
      }),
    ];
    const groups = groupGames(games);
    expect(groups[0].primary.store).toBe("gog");
  });

  it("falls back to fixed store priority when nothing is installed", () => {
    const games = [
      g({ store: "microsoft", dedupe_group_id: "x" }),
      g({ store: "epic", dedupe_group_id: "x" }),
    ];
    const groups = groupGames(games);
    expect(groups[0].primary.store).toBe("epic");
  });

  it("preserves first-appearance order of groups", () => {
    const first = g({ store: "epic" });
    const games = [
      first,
      g({ store: "gog", dedupe_group_id: "x" }),
      g({ store: "amazon", dedupe_group_id: "x" }),
    ];
    const groups = groupGames(games);
    expect(groups.map((grp) => grp.key)).toEqual([String(first.app_id), "x"]);
  });
});

describe("STORE_PRIORITY", () => {
  it("covers every StoreId variant — D.16 regression", () => {
    // itch was missing from this list, which made `indexOf` return -1
    // for it — see `storePriorityRank`'s docstring for why that sorted
    // itch FIRST rather than last.
    for (const store of ALL_STORE_IDS) {
      expect(STORE_PRIORITY).toContain(store);
    }
  });
});

describe("storePriorityRank", () => {
  it("ranks known stores by their STORE_PRIORITY index", () => {
    expect(storePriorityRank("steam")).toBe(0);
    expect(storePriorityRank("itch")).toBe(STORE_PRIORITY.indexOf("itch"));
  });

  it("ranks an unknown store LAST, not first", () => {
    const unknownRank = storePriorityRank("nonexistent-store" as StoreId);
    expect(unknownRank).toBe(Number.POSITIVE_INFINITY);
    for (const store of STORE_PRIORITY) {
      expect(storePriorityRank(store)).toBeLessThan(unknownRank);
    }
  });
});
