/**
 * game-grouping — client-side grouping of cross-store duplicate games.
 *
 * Pure display logic: groups the flat `Game[]` from `get_all_unifideck_games`
 * by the `dedupe_group_id` the backend already computed (see
 * `core/game_grouping.py` — title matching stays server-side, this module
 * never re-derives it). Used by `GameGrid` to render one tile per group with
 * a multi-store badge cluster.
 */
import type { Game, StoreId } from "../types/api";

export interface GameGroup {
  /** `dedupe_group_id` for a real group, or a per-game fallback key for
   *  an ungrouped (singleton) game — always unique, safe as a React key. */
  key: string;
  games: Game[];
  /** The tile's default store: installed copy first, else the first
   *  store found in `STORE_PRIORITY`. */
  primary: Game;
}

/** Stable per-game identity for the singleton-group fallback key.
 *
 * `Game.id` looks like the obvious choice but is never actually present
 * on this RPC path — the backend `Game` dataclass has no `id` field
 * (only `app_id`/`store`/`store_game_id`), and this route returns raw
 * `asdict()` rows with no frontend adaptation (see the wire-field note
 * on `UnifideckCacheEntry` in `lib/library-filters`). Using `game.id`
 * here meant every ungrouped game evaluated to the same `undefined`
 * key and got merged into one bucket — the exact bug this fallback
 * exists to avoid. `app_id` is backend-guaranteed unique per game; the
 * store/store_game_id pair is the fallback if it's ever missing.
 */
export function gameKey(game: Game): string {
  if (game.app_id != null) return String(game.app_id);
  return `${game.store}:${game.store_game_id}`;
}

/** Fixed tie-break order when no copy in a group is installed. `"steam"`
 *  leads only for forward-compatibility — the backend never returns a
 *  native Steam entry today, so it's a no-op until/unless that changes.
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
 *  of falling to the back where an "unranked" store belongs. Centralised
 *  here so every caller (this module's `pickPrimary`,
 *  `GameStoreSwitcher`'s `sortByStorePriority`,
 *  `library-filters`'s `pickGroupPrimary`) gets the fix at once and the
 *  next added `StoreId` degrades gracefully even if `STORE_PRIORITY`
 *  itself isn't updated in the same change. */
export function storePriorityRank(store: StoreId): number {
  const index = STORE_PRIORITY.indexOf(store);
  return index === -1 ? Number.POSITIVE_INFINITY : index;
}

function pickPrimary(games: Game[]): Game {
  const installed = games.find((g) => g.installed ?? g.is_installed);
  if (installed) return installed;
  for (const store of STORE_PRIORITY) {
    const match = games.find((g) => g.store === store);
    if (match) return match;
  }
  return games[0];
}

/** Groups `games` by `dedupe_group_id`, falling back to a stable
 *  per-game key ({@link gameKey}) so ungrouped games become singleton
 *  groups — callers never need to special-case "no duplicates found".
 *  Order of first appearance is preserved. */
export function groupGames(games: Game[]): GameGroup[] {
  const order: string[] = [];
  const byKey = new Map<string, Game[]>();
  for (const game of games) {
    const key = game.dedupe_group_id ?? gameKey(game);
    const existing = byKey.get(key);
    if (existing) {
      existing.push(game);
    } else {
      byKey.set(key, [game]);
      order.push(key);
    }
  }
  return order.map((key) => {
    const groupGamesForKey = byKey.get(key)!;
    return {
      key,
      games: groupGamesForKey,
      primary: pickPrimary(groupGamesForKey),
    };
  });
}
