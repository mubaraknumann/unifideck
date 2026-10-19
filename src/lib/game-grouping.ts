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

/** The items whose title isn't tagged "Xbox One"-only. A titleless item counts
 *  as tagged, since it never outranks a real title. An unsuffixed or
 *  "Series X|S" title outranks the older console's tag: the Series X|S copy
 *  plays on both consoles and reads as the more current listing. */
export function nonXboxOneTagged<T extends { title?: string }>(
  items: readonly T[],
): T[] {
  return items.filter(
    (i) => i.title != null && !XBOX_ONE_ONLY_TAG.test(i.title),
  );
}

/** A member that may carry the rank its store gave it (`metadata.default_rank`,
 *  lower wins). It only breaks ties between a store's own members. */
export interface HasDefaultRank {
  defaultRank?: number;
}

/** A store's `default_rank` from a row's `metadata`. Anything other than a
 *  finite number >= 0 reads as unranked. */
export function readDefaultRank(
  metadata: Record<string, unknown> | undefined,
): number | undefined {
  const value = metadata?.default_rank;
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : undefined;
}

/** Orders by ascending rank, with unranked members last. Equal ranks compare
 *  as 0, so a stable sort keeps their order. */
export function compareByDefaultRank(
  a: HasDefaultRank,
  b: HasDefaultRank,
): number {
  const left = a.defaultRank ?? Number.POSITIVE_INFINITY;
  const right = b.defaultRank ?? Number.POSITIVE_INFINITY;
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

/** The lowest-ranked item, or the earliest among equals; `undefined` for an
 *  empty list. With no ranks at all it is the first item. */
export function firstByDefaultRank<T extends HasDefaultRank>(
  items: readonly T[],
): T | undefined {
  let best: T | undefined;
  for (const item of items) {
    if (best === undefined || compareByDefaultRank(item, best) < 0) best = item;
  }
  return best;
}

/** The switcher's listing order: store priority, then members without an
 *  edition label, then rank. Ties keep their incoming order. */
export function sortByStoreAndRank<
  T extends { store: StoreId; editionLabel?: string; defaultRank?: number },
>(siblings: readonly T[]): T[] {
  return [...siblings].sort(
    (a, b) =>
      storePriorityRank(a.store) - storePriorityRank(b.store) ||
      Number(a.editionLabel != null) - Number(b.editionLabel != null) ||
      compareByDefaultRank(a, b),
  );
}
