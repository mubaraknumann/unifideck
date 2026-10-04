import { describe, expect, it } from "vitest";
import { indexOfFirstNonXboxOneTagged, storePriorityRank, STORE_PRIORITY } from "./game-grouping";
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
