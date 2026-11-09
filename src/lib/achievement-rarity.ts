/**
 * Achievement rarity, read the way Steam reads it.
 *
 * Steam draws its animated amber glow around an achievement icon when the
 * achievement is unlocked and fewer than 10% of players have it. Measured
 * in Steam's bundle on 2026-11-02: `glow: bAchieved && flAchieved < k` with
 * `const k = 10`. The same rule here keeps our tiles in step with Steam's.
 */
import type { Achievement } from "../types/api";

/** Below this share of players, an unlocked achievement glows. */
export const RARE_PERCENT = 10;

/** Whether Steam would draw the rare glow for this achievement. */
export function isRareAchievement(ach: Achievement): boolean {
  return ach.unlocked && ach.rarity != null && ach.rarity < RARE_PERCENT;
}

/** Rarity rounded to one decimal, as Steam shows it, or null when unknown. */
export function rarityPercent(ach: Achievement): number | null {
  return ach.rarity == null ? null : Math.round(ach.rarity * 10) / 10;
}
