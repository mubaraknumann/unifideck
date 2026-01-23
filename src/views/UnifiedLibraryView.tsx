import React, { FC, useMemo, useState } from "react";
import { useSteamLibrary, useUnifideckGames } from "../hooks/useSteamLibrary";
import { GameGrid } from "../components/GameGrid";
import { StoreType } from "../types/steam";

export type LibraryFilter = "all" | "installed" | "great-on-deck";

interface UnifiedLibraryViewProps {
  filter: LibraryFilter;
}

// Error boundary wrapper
class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean; error?: Error }
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error("[Unifideck] UnifiedLibraryView error:", error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: "20px", color: "#ff6b6b" }}>
          <h3>Unifideck Error</h3>
          <p>Failed to load unified library view</p>
          <pre style={{ fontSize: "11px", opacity: 0.7 }}>
            {this.state.error?.message}
          </pre>
          <p style={{ fontSize: "12px", opacity: 0.7, marginTop: "10px" }}>
            Check browser console for details
          </p>
        </div>
      );
    }

    return this.props.children;
  }
}

/**
 * Unified library view that shows games from all stores
 * Replaces Steam's default All Games, Installed, and Great on Deck tabs
 */
const UnifiedLibraryViewInner: FC<UnifiedLibraryViewProps> = ({ filter }) => {
  console.log(
    `[Unifideck] Rendering UnifiedLibraryView with filter: ${filter}`,
  );
  const { games, loading, error } = useSteamLibrary();
  const { gameMetadata, getStoreForApp } = useUnifideckGames();
  const [storeFilter, setStoreFilter] = useState<StoreType | "all">("all");
  const [searchQuery, setSearchQuery] = useState("");

  // Enhance games with Unifideck metadata (store info)
  const enhancedGames = useMemo(() => {
    const enhanced = games.map((game) => {
      const store = getStoreForApp(game.appId);
      if (store) {
        return { ...game, store };
      }

      // Log games without metadata
      if (game.store === "unknown" || !game.store) {
        console.log(
          `[Unifideck] No metadata for game ${game.appId}: ${game.title} (original store: ${game.store})`,
        );
      }

      return game;
    });

    console.log(
      `[Unifideck] Enhanced ${enhanced.length} games. Store breakdown:`,
    );
    const storeCounts = enhanced.reduce((acc, g) => {
      acc[g.store || "undefined"] = (acc[g.store || "undefined"] || 0) + 1;
      return acc;
    }, {} as Record<string, number>);
    console.log("[Unifideck]", storeCounts);

    return enhanced;
  }, [games, gameMetadata]);

  // Apply filters
  const filteredGames = useMemo(() => {
    let result = [...enhancedGames];

    // Apply main filter (all/installed/great-on-deck)
    switch (filter) {
      case "installed":
        result = result.filter((game) => game.isInstalled);
        break;
      case "great-on-deck":
        result = result.filter(
          (game) =>
            game.deckVerified === "verified" ||
            game.deckVerified === "playable",
        );
        break;
      case "all":
      default:
        // Show all games
        break;
    }

    // Apply store filter
    if (storeFilter !== "all") {
      result = result.filter((game) => game.store === storeFilter);
    }

    // Apply search filter
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      result = result.filter((game) =>
        game.title.toLowerCase().includes(query),
      );
    }

    // Sort by title
    result.sort((a, b) => a.title.localeCompare(b.title));

    return result;
  }, [enhancedGames, filter, storeFilter, searchQuery]);

  if (error) {
    return (
      <div
        style={{
          padding: "20px",
          textAlign: "center",
          color: "#ff6b6b",
        }}
      >
        <div style={{ marginBottom: "10px", fontSize: "16px" }}>
          Error loading games
        </div>
        <div style={{ fontSize: "12px", opacity: 0.7 }}>{error}</div>
        <div style={{ marginTop: "15px", fontSize: "11px", opacity: 0.5 }}>
          Try reloading the plugin or checking the console for details
        </div>
      </div>
    );
  }

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
      }}
    >
      {/* Header with filters */}
      <div
        style={{
          padding: "15px 20px",
          background: "rgba(0, 0, 0, 0.3)",
          borderBottom: "1px solid rgba(255, 255, 255, 0.1)",
        }}
      >
        <div
          style={{
            display: "flex",
            gap: "15px",
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          {/* Title */}
          <div style={{ fontSize: "18px", fontWeight: "bold" }}>
            {getFilterTitle(filter)}
          </div>

          {/* Store filter */}
          <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
            <span style={{ fontSize: "12px", opacity: 0.7 }}>Store:</span>
            <select
              value={storeFilter}
              onChange={(e) =>
                setStoreFilter(e.target.value as StoreType | "all")
              }
              style={{
                background: "rgba(255, 255, 255, 0.1)",
                border: "1px solid rgba(255, 255, 255, 0.2)",
                borderRadius: "4px",
                padding: "4px 8px",
                color: "white",
                fontSize: "12px",
              }}
            >
              <option value="all">All Stores</option>
              <option value="steam">Steam</option>
              <option value="epic">Epic Games</option>
              <option value="gog">GOG</option>
              <option value="amazon">Amazon Games</option>
            </select>
          </div>

          {/* Search */}
          <input
            type="text"
            placeholder="Search games..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              background: "rgba(255, 255, 255, 0.1)",
              border: "1px solid rgba(255, 255, 255, 0.2)",
              borderRadius: "4px",
              padding: "6px 12px",
              color: "white",
              fontSize: "12px",
              flex: "1",
              minWidth: "200px",
            }}
          />
        </div>
      </div>

      {/* Game grid */}
      <div style={{ flex: 1, overflow: "auto" }}>
        <GameGrid games={filteredGames} loading={loading} />
      </div>
    </div>
  );
};

// Wrapped export with error boundary
export const UnifiedLibraryView: FC<UnifiedLibraryViewProps> = (props) => {
  return (
    <ErrorBoundary>
      <UnifiedLibraryViewInner {...props} />
    </ErrorBoundary>
  );
};

function getFilterTitle(filter: LibraryFilter): string {
  switch (filter) {
    case "all":
      return "All Games";
    case "installed":
      return "Installed";
    case "great-on-deck":
      return "Great on Deck";
    default:
      return "Library";
  }
}
