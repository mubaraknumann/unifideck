/**
 * GameGrid — responsive grid of game cover tiles.
 *
 * Pure presentational : receives an array of `Game` and an
 * `onSelect(appId)` callback. Each tile renders the cover
 * image, the title (truncated), and a store-badge cluster
 * (single icon, or up to 3 + overflow when the game groups
 * several stores' copies into one card — see `game-grouping`).
 *
 * Used by the unified library view and any future grid-
 * style picker (e.g. multi-select for batch operations).
 */
import { FC, useMemo } from "react";
import { Focusable, DialogButton } from "@decky/ui";
import { StoreIcon } from "./StoreIcon";
import { groupGames, gameKey, type GameGroup } from "../../lib/game-grouping";
import type { Game } from "../../types/api";

const MAX_VISIBLE_STORE_ICONS = 3;

/** Props. */
interface Props {
  games: Game[];
  onSelect: (game: Game) => void;
  tileWidth?: number;
  /** Merge cross-store duplicates into one tile with a badge cluster.
   *  Default true; pass false when the caller has already narrowed the
   *  list to a single store (grouping would be a no-op that still costs
   *  a pass over the list). */
  groupingEnabled?: boolean;
}

/**
 * Generic responsive grid of {@link UnifideckGame} cards.
 * Used by every list view (per-store, search results,
 * recently played). Virtualised under a configurable
 * threshold to keep memory bounded on large libraries.
 */
export const GameGrid: FC<Props> = ({
  games,
  onSelect,
  tileWidth = 140,
  groupingEnabled = true,
}) => {
  const groups = useMemo<GameGroup[]>(
    () =>
      groupingEnabled
        ? groupGames(games)
        : games.map((game) => ({
            key: gameKey(game),
            games: [game],
            primary: game,
          })),
    [games, groupingEnabled],
  );

  return (
    <Focusable
      flow-children="grid"
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(auto-fill, minmax(${tileWidth}px, 1fr))`,
        gap: 12,
        padding: 12,
      }}
    >
      {groups.map((group) => {
        const game = group.primary;
        const visibleStores = group.games.slice(0, MAX_VISIBLE_STORE_ICONS);
        const overflowCount = group.games.length - visibleStores.length;
        return (
          <DialogButton
            key={group.key}
            onClick={() => onSelect(group.primary)}
            style={{
              position: "relative",
              background: "transparent",
              border: "none",
              padding: 0,
              display: "flex",
              flexDirection: "column",
              gap: 4,
            }}
          >
            {game.cover_image && (
              <img
                src={game.cover_image}
                alt={game.title}
                style={{
                  width: "100%",
                  aspectRatio: "2 / 3",
                  borderRadius: 6,
                  objectFit: "cover",
                }}
              />
            )}
            <span
              style={{
                fontSize: 12,
                color: "#e5e7eb",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
                textAlign: "left",
              }}
            >
              {game.title}
            </span>
            <span
              style={{
                position: "absolute",
                top: 6,
                insetInlineEnd: 6,
                display: "flex",
                gap: 3,
                background: "#0f172acc",
                borderRadius: 3,
                padding: 3,
              }}
            >
              {visibleStores.map((g) => (
                <StoreIcon
                  key={`${g.store}-${g.app_id ?? g.store_game_id}`}
                  store={g.store}
                  size={12}
                />
              ))}
              {overflowCount > 0 && (
                <span
                  style={{ fontSize: 10, color: "#e5e7eb", lineHeight: "12px" }}
                >
                  +{overflowCount}
                </span>
              )}
            </span>
          </DialogButton>
        );
      })}
    </Focusable>
  );
};
