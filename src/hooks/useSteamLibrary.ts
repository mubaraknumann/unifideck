import { useState, useEffect } from "react";
import { call } from "@decky/api";
import { SteamApp, UnifideckGame, StoreType } from "../types/steam";

/**
 * Hook to access Steam's game library including non-Steam games
 * Uses Steam's global APIs to query the game collection
 */
export function useSteamLibrary() {
  const [games, setGames] = useState<UnifideckGame[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadGames();
  }, []);

  const loadGames = async () => {
    try {
      setLoading(true);
      setError(null);

      // Check if Steam Client APIs are available
      if (!window.SteamClient?.Apps) {
        throw new Error("SteamClient.Apps API not available");
      }

      // Get all games (Steam + non-Steam)
      const ownedApps = window.SteamClient.Apps.GetOwnedApps() || [];
      const nonSteamApps = window.SteamClient.Apps.GetNonSteamApps() || [];

      console.log("[Unifideck] Loaded owned apps:", ownedApps.length);
      console.log("[Unifideck] Loaded non-Steam apps:", nonSteamApps.length);

      // Combine and transform
      const allApps = [...ownedApps, ...nonSteamApps];
      const unifideckGames: UnifideckGame[] = allApps.map((app) =>
        transformSteamApp(app)
      );

      setGames(unifideckGames);
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : "Unknown error";
      console.error("[Unifideck] Error loading games:", errorMsg);
      setError(errorMsg);
    } finally {
      setLoading(false);
    }
  };

  const transformSteamApp = (app: SteamApp): UnifideckGame => {
    const isShortcut = app.is_shortcuts_app || app.BIsShortcut?.();
    const store = detectStore(app);

    return {
      appId: app.appid,
      title: app.display_name || app.sort_as || "Unknown",
      store,
      isInstalled: app.installed || false,
      isShortcut,
      lastPlayed: app.rt_last_time_played || 0,
      playtimeMinutes: parseInt(app.minutes_playtime_forever || "0", 10),
      deckVerified: "unknown", // TODO: Add ProtonDB integration
    };
  };

  const detectStore = (app: SteamApp): StoreType => {
    // For shortcuts, check LaunchOptions or tags to determine store
    // This requires accessing the app overview for more details
    if (app.is_shortcuts_app || app.BIsShortcut?.()) {
      const overview = window.appStore?.GetAppOverviewByAppID(app.appid);
      if (overview) {
        // Check for Epic/GOG markers we added in shortcuts.vdf
        // We store the store info in LaunchOptions: "epic:game_id" or "gog:game_id"
        // But we can't access LaunchOptions from the app object directly
        // So we rely on tags instead
        // For now, mark as unknown and we'll improve this
        return "unknown";
      }
      return "unknown";
    }

    // Native Steam games
    return "steam";
  };

  const filterByInstalled = (gameList: UnifideckGame[]) => {
    return gameList.filter((game) => game.isInstalled);
  };

  const filterByStore = (gameList: UnifideckGame[], store: StoreType) => {
    return gameList.filter((game) => game.store === store);
  };

  const filterByDeckVerified = (gameList: UnifideckGame[]) => {
    return gameList.filter(
      (game) => game.deckVerified === "verified" || game.deckVerified === "playable"
    );
  };

  return {
    games,
    loading,
    error,
    refresh: loadGames,
    filters: {
      installed: filterByInstalled,
      byStore: filterByStore,
      deckVerified: filterByDeckVerified,
    },
  };
}

/**
 * Hook to query games added by Unifideck via backend
 * This provides store information that we can't get from Steam APIs alone
 */
export function useUnifideckGames() {
  const [gameMetadata, setGameMetadata] = useState<Map<number, { store: StoreType }>>(
    new Map()
  );

  useEffect(() => {
    loadUnifideckMetadata();
  }, []);

  const loadUnifideckMetadata = async () => {
    try {
      // Call backend to get mapping of appId -> store
      const result = await call<[], Record<number, string>>(
        "get_game_metadata"
      );

      console.log(`[Unifideck] Loaded metadata for ${Object.keys(result || {}).length} games`);
      console.log("[Unifideck] Metadata sample:", Object.entries(result || {}).slice(0, 5));

      if (result) {
        const metadata = new Map<number, { store: StoreType }>();
        Object.entries(result).forEach(([appIdStr, store]) => {
          metadata.set(parseInt(appIdStr, 10), { store: store as StoreType });
        });
        setGameMetadata(metadata);
      }
    } catch (error) {
      console.error("[Unifideck] Error loading game metadata:", error);
    }
  };

  const getStoreForApp = (appId: number): StoreType | null => {
    return gameMetadata.get(appId)?.store || null;
  };

  return {
    gameMetadata,
    getStoreForApp,
    refresh: loadUnifideckMetadata,
  };
}
