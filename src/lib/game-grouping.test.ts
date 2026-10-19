import { describe, expect, it } from "vitest";
import {
  compareByDefaultRank,
  firstByDefaultRank,
  indexOfFirstNonXboxOneTagged,
  readDefaultRank,
  storePriorityRank,
  STORE_PRIORITY,
} from "./game-grouping";
import type { StoreId } from "../types/api";

// Every `StoreId` member, kept in sync by hand with `types/api.ts` —
// there's no runtime array to derive this from (it's a type), so this
// list is the regression guard: a `StoreId` added without a matching
// `STORE_PRIORITY` entry used to sort FIRST, not last (see
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

describe("STORE_PRIORITY", () => {
  it("covers every StoreId variant", () => {
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

describe("indexOfFirstNonXboxOneTagged", () => {
  it("prefers the Series X|S or unsuffixed title over an Xbox One tag", () => {
    expect(indexOfFirstNonXboxOneTagged(["NHL 24 Xbox One", "NHL 24 Xbox Series X|S"])).toBe(1);
    expect(indexOfFirstNonXboxOneTagged(["Enlisted Xbox One", "Enlisted"])).toBe(1);
  });

  it("returns -1 when every candidate is Xbox One tagged or untitled", () => {
    expect(indexOfFirstNonXboxOneTagged(["Enlisted Xbox One", undefined])).toBe(-1);
  });
});

describe("default rank", () => {
  it("reads only finite non-negative numbers", () => {
    expect(readDefaultRank({ default_rank: 2 })).toBe(2);
    expect(readDefaultRank({ default_rank: 0 })).toBe(0);
    for (const bad of [undefined, null, "1", Number.NaN, Number.POSITIVE_INFINITY, -1]) {
      expect(readDefaultRank({ default_rank: bad })).toBeUndefined();
    }
    expect(readDefaultRank(undefined)).toBeUndefined();
    expect(readDefaultRank({})).toBeUndefined();
  });

  it("compares lower first, ranked before unranked, ties as zero", () => {
    expect(compareByDefaultRank({ defaultRank: 0 }, { defaultRank: 1 })).toBeLessThan(0);
    expect(compareByDefaultRank({ defaultRank: 5 }, {})).toBeLessThan(0);
    expect(compareByDefaultRank({}, { defaultRank: 5 })).toBeGreaterThan(0);
    expect(compareByDefaultRank({}, {})).toBe(0);
    expect(compareByDefaultRank({ defaultRank: 2 }, { defaultRank: 2 })).toBe(0);
  });

  it("picks the lowest rank and the earliest among equals", () => {
    expect(firstByDefaultRank([])).toBeUndefined();
    type Item = { id: string; defaultRank?: number };
    const items: Item[] = [{ id: "a" }, { id: "b", defaultRank: 1 }, { id: "c", defaultRank: 0 }];
    expect(firstByDefaultRank(items)?.id).toBe("c");
    expect(firstByDefaultRank<Item>([{ id: "a" }, { id: "b" }])?.id).toBe("a");
    expect(
      firstByDefaultRank<Item>([{ id: "a", defaultRank: 1 }, { id: "b", defaultRank: 1 }])?.id,
    ).toBe("a");
  });
});
