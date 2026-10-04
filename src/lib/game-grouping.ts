/**
 * game-grouping — store order and tie-breaks for cross-store duplicates.
 *
 * Which games are duplicates is decided server-side (`core/game_identity.py`,
 * stamped as `dedupe_group_id` by `core/game_grouping.py`); this module never
 * re-derives it. It only answers "which copy goes first": the store switcher
 * on a game page (`GameStoreSwitcher`) sorts by it, and the library tab
 * filters (`library-filters`'s `pickGroupPrimary`) pick the tile shown for a
 * group with it.
 */
import type { StoreId } from "../types/api";

/** Fixed order when no copy in a group is installed. Steam leads: a game the
 *  user owns on Steam is represented by its native Steam tile.
 *
 *  Every `StoreId` variant MUST appear here — see
 *  {@link storePriorityRank}'s docstring for why an omission used to be
 *  worse than a no-op (itch sorted FIRST, not last, until this list
 *  caught up with `StoreId` gaining an `"itch"` member). A unit test
 *  asserts this list stays exhaustive. */
export const STORE_PRIORITY: StoreId[] = [
  "steam",
  "epic",
  "gog",
  "amazon",
  "ubisoft",
  "battlenet",
  "microsoft",
  "gamevault",
  "itch",
];

/** `STORE_PRIORITY`'s rank for `store`, or `+Infinity` when it's missing
 *  from that list.
 *
 *  `Array.indexOf` returns `-1` for a miss, and `-1` sorts BEFORE every
 *  real index in a plain `a - b` comparator — so a `StoreId` added
 *  after `STORE_PRIORITY` was last updated (this happened with
 *  `"itch"`) silently jumped to the FRONT of every sorted list instead
 *  of falling to the back where an "unranked" store belongs. */
export function storePriorityRank(store: StoreId): number {
  const index = STORE_PRIORITY.indexOf(store);
  return index === -1 ? Number.POSITIVE_INFINITY : index;
}

/** True when ``title`` carries an "Xbox One"-only console tag. Matched only
 *  to rank it below an unsuffixed or "Series X|S" sibling from the same
 *  store; it never affects grouping. */
const XBOX_ONE_ONLY_TAG = /\bxbox\s+one(?:\s+(?:edition|version))?\s*$/i;

/** Index of the first candidate whose title ISN'T tagged "Xbox One"-only,
 *  or `-1` when every candidate is (or none has a title to check — a
 *  titleless entry never outranks a real one, so it's treated the same as
 *  "tagged"). An unsuffixed title or an explicit "Series X|S" release both
 *  outrank the older console's tag: the Series X|S copy plays on both
 *  consoles and reads as the more current listing. */
export function indexOfFirstNonXboxOneTagged(
  titles: (string | undefined)[],
): number {
  return titles.findIndex((t) => t != null && !XBOX_ONE_ONLY_TAG.test(t));
}
