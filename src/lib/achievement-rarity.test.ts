/**
 * The rare-glow rule mirrors Steam's: unlocked and under 10% of players.
 */
import { describe, it, expect } from "vitest";
import { isRareAchievement, rarityPercent } from "./achievement-rarity";
import type { Achievement } from "../types/api";

function ach(unlocked: boolean, rarity: number | null | undefined): Achievement {
  return {
    key: "k",
    name: "n",
    description: "",
    image_unlocked: "",
    image_locked: "",
    hidden: false,
    unlocked,
    unlocked_at: unlocked ? 1 : null,
    rarity,
  };
}

describe("isRareAchievement", () => {
  it("glows only when unlocked and under 10%", () => {
    expect(isRareAchievement(ach(true, 5.85))).toBe(true);
    expect(isRareAchievement(ach(true, 9.99))).toBe(true);
    expect(isRareAchievement(ach(true, 10))).toBe(false);
    expect(isRareAchievement(ach(false, 1))).toBe(false);
    expect(isRareAchievement(ach(true, null))).toBe(false);
    expect(isRareAchievement(ach(true, undefined))).toBe(false);
  });
});

describe("rarityPercent", () => {
  it("rounds to one decimal and keeps unknown as null", () => {
    expect(rarityPercent(ach(true, 82.85))).toBe(82.9);
    expect(rarityPercent(ach(false, 0))).toBe(0);
    expect(rarityPercent(ach(false, null))).toBeNull();
  });
});
