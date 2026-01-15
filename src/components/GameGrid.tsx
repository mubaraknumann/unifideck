import { FC } from "react";
import { t } from "../i18n";
import { UnifideckGame } from "../types/steam";

interface GameGridProps {
  games: UnifideckGame[];
  loading?: boolean;
}

/**
 * GameGrid component that leverages Steam's native game rendering
 * Instead of creating custom game cards, we navigate to Steam's library
 * and let Steam handle the rendering using its native components
 */
export const GameGrid: FC<GameGridProps> = ({ games, loading }) => {
  if (loading) {
    return (
      <div
        style={{
          display: "flex",
          justifyContent: "center",
          alignItems: "center",
          height: "400px",
          fontSize: "16px",
          opacity: 0.7,
        }}
      >
        {t("gameGrid.loading")}
      </div>
    );
  }

  if (games.length === 0) {
    return (
      <div
        style={{
          display: "flex",
          justifyContent: "center",
          alignItems: "center",
          height: "400px",
          fontSize: "16px",
          opacity: 0.7,
          flexDirection: "column",
          gap: "10px",
        }}
      >
        <div>{t("gameGrid.noGamesFound")}</div>
        <div style={{ fontSize: "12px" }}>
          {t("gameGrid.noGamesHint")}
        </div>
      </div>
    );
  }

  /**
   * IMPORTANT: This is a placeholder approach
   *
   * The ideal implementation would use Steam's native GameTile component:
   * - Access via: findModuleChild() or similar from decky-frontend-lib
   * - Render native tiles: <GameTile appid={game.appId} />
   *
   * However, since Steam's internal components are not officially documented,
   * we use a simpler approach: Display a list and rely on Steam's existing
   * library view to handle the actual game rendering.
   *
   * This component serves as a filter/organizer rather than a renderer.
   */
  return (
    <div
      style={{
        padding: "20px",
        display: "flex",
        flexDirection: "column",
        gap: "10px",
      }}
    >
      <div
        style={{
          fontSize: "14px",
          opacity: 0.7,
          marginBottom: "10px",
        }}
      >
        {t("gameGrid.showingGames", { count: games.length })}
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
          gap: "15px",
        }}
      >
        {games.map((game) => (
          <GameCard key={game.appId} game={game} />
        ))}
      </div>
    </div>
  );
};

/**
 * Simple game card component
 * In production, this should be replaced with Steam's native GameTile component
 */
const GameCard: FC<{ game: UnifideckGame }> = ({ game }) => {
  const handleClick = () => {
    // Navigate to game details in Steam
    window.location.href = `steam://nav/library/app/${game.appId}`;
  };

  return (
    <div
      onClick={handleClick}
      style={{
        background: "rgba(255, 255, 255, 0.05)",
        borderRadius: "4px",
        padding: "15px",
        cursor: "pointer",
        transition: "background 0.2s",
        border: "1px solid rgba(255, 255, 255, 0.1)",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.background = "rgba(255, 255, 255, 0.1)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = "rgba(255, 255, 255, 0.05)";
      }}
    >
      <div style={{ marginBottom: "8px", fontWeight: "bold" }}>
        {game.title}
      </div>

      <div
        style={{
          display: "flex",
          gap: "8px",
          fontSize: "11px",
          flexWrap: "wrap",
        }}
      >
        {game.store !== "steam" && game.store !== "unknown" && (
          <span
            style={{
              background: getStoreColor(game.store),
              padding: "2px 6px",
              borderRadius: "3px",
            }}
          >
            {game.store.toUpperCase()}
          </span>
        )}

        {game.isInstalled && (
          <span
            style={{
              background: "rgba(76, 175, 80, 0.3)",
              padding: "2px 6px",
              borderRadius: "3px",
            }}
          >
            {t("gameGrid.installed")}
          </span>
        )}

        {!game.isInstalled && game.isShortcut && (
          <span
            style={{
              background: "rgba(255, 152, 0, 0.3)",
              padding: "2px 6px",
              borderRadius: "3px",
            }}
          >
            {t("gameGrid.notInstalled")}
          </span>
        )}
      </div>

      {(game.playtimeMinutes ?? 0) > 0 && (
        <div style={{ marginTop: "8px", fontSize: "11px", opacity: 0.7 }}>
          {Math.floor((game.playtimeMinutes ?? 0) / 60)}h{" "}
          {(game.playtimeMinutes ?? 0) % 60}m played
        </div>
      )}
    </div>
  );
};

function getStoreColor(store: string): string {
  switch (store) {
    case "epic":
      return "rgba(0, 120, 215, 0.4)"; // Epic blue
    case "gog":
      return "rgba(156, 39, 176, 0.4)"; // GOG purple
    default:
      return "rgba(255, 255, 255, 0.2)";
  }
}
