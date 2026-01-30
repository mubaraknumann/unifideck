import { call } from "@decky/api";

// Cache for shortcut -> real Steam App ID mappings
let steamAppIdMappings: Record<number, number> = {};

// Cache for fetched store data (to avoid repeated API calls)
let storeDataCache: Record<number, any> = {};

/**
 * Load the Steam App ID mappings from backend.
 * Call this during plugin initialization.
 */
export async function loadSteamAppIdMappings(): Promise<void> {
  try {
    const result = await call<
      [],
      { success: boolean; mappings: Record<number, number> }
    >("get_real_steam_appid_mappings");

    if (result?.success) {
      steamAppIdMappings = result.mappings;
      console.log(
        `[Unifideck Store Patch] Loaded ${Object.keys(steamAppIdMappings).length} Steam App ID mappings`,
      );
    }
  } catch (error) {
    console.error("[Unifideck Store Patch] Failed to load mappings:", error);
  }
}

/**
 * Fetch game data from Steam's public web API.
 * Works for ANY app ID, regardless of ownership.
 */
async function fetchSteamStoreData(steamAppId: number): Promise<any | null> {
  // Check cache first
  if (storeDataCache[steamAppId]) {
    return storeDataCache[steamAppId];
  }

  try {
    const response = await fetch(
      `https://store.steampowered.com/api/appdetails?appids=${steamAppId}&l=english`,
    );
    const data = await response.json();

    if (data[steamAppId]?.success && data[steamAppId]?.data) {
      const gameData = data[steamAppId].data;

      // Construct a SteamAppOverview-compatible object from web API data
      const overview = {
        appid: steamAppId,
        display_name: gameData.name,
        // Map web API data to overview structure
        rt_original_release_date: gameData.release_date?.date
          ? Math.floor(new Date(gameData.release_date.date).getTime() / 1000)
          : 0,
        steam_deck_compat_category: gameData.steam_deck_compatibility || 0,
        controller_support:
          gameData.controller_support === "full"
            ? 2
            : gameData.controller_support === "partial"
              ? 1
              : 0,
        // Add more fields as needed from web API response
        __from_web_api: true, // Flag to indicate this is from web API
        __web_api_data: gameData, // Store full data for details panels
      };

      storeDataCache[steamAppId] = overview;
      return overview;
    }

    return null;
  } catch (error) {
    console.error(
      `[Unifideck Store Patch] Failed to fetch store data for ${steamAppId}:`,
      error,
    );
    return null;
  }
}

/**
 * Patch Steam's global data stores to return real Steam game data.
 * For owned games: Returns library data (fast).
 * For unowned games: Fetches from web API (slower, but works).
 */
export function patchSteamStores(): () => void {
  const appStore = window.appStore;
  const appDetailsStore = window.appDetailsStore;

  if (!appStore || !appDetailsStore) {
    console.error("[Unifideck Store Patch] Steam stores not available");
    return () => {};
  }

  // Store original methods
  const origGetAppOverview = appStore.GetAppOverviewByAppID;
  const origGetAppDetails = appDetailsStore.GetAppDetails;
  const origGetAppData = appDetailsStore.GetAppData;

  // Patch GetAppOverviewByAppID (used by most UI components)
  appStore.GetAppOverviewByAppID = function (appId: number) {
    const realSteamAppId = steamAppIdMappings[appId];
    if (!realSteamAppId) {
      // Not a Unifideck shortcut, return original
      return origGetAppOverview.call(this, appId);
    }

    // Try owned game first (fast)
    const ownedData = origGetAppOverview.call(this, realSteamAppId);
    if (ownedData) {
      console.log(
        `[Unifideck Store Patch] Owned game: ${appId} -> ${realSteamAppId}`,
      );
      return ownedData;
    }

    // Not owned - fetch from web API (async, returns Promise)
    console.log(
      `[Unifideck Store Patch] Unowned game, fetching from web API: ${realSteamAppId}`,
    );

    // IMPORTANT: appStore methods are synchronous but we need async fetch
    // Return a promise that Steam's components should handle
    // If components don't support promises, return null and inject custom component instead
    return fetchSteamStoreData(realSteamAppId);
  };

  // Patch GetAppDetails (similar logic)
  appDetailsStore.GetAppDetails = function (appId: number) {
    const realSteamAppId = steamAppIdMappings[appId];
    if (!realSteamAppId) {
      return origGetAppDetails.call(this, appId);
    }

    const ownedDetails = origGetAppDetails.call(this, realSteamAppId);
    if (ownedDetails) {
      return ownedDetails;
    }

    // For unowned games, construct details from web API data
    const webData = fetchSteamStoreData(realSteamAppId);
    return webData;
  };

  // Patch GetAppData
  appDetailsStore.GetAppData = function (appId: number) {
    const realSteamAppId = steamAppIdMappings[appId];
    if (!realSteamAppId) {
      return origGetAppData.call(this, appId);
    }

    const ownedData = origGetAppData.call(this, realSteamAppId);
    if (ownedData) {
      return ownedData;
    }

    return fetchSteamStoreData(realSteamAppId);
  };

  console.log("[Unifideck Store Patch] Steam stores patched successfully");

  // Return unpatch function
  return () => {
    appStore.GetAppOverviewByAppID = origGetAppOverview;
    appDetailsStore.GetAppDetails = origGetAppDetails;
    appDetailsStore.GetAppData = origGetAppData;
    storeDataCache = {}; // Clear cache
    console.log("[Unifideck Store Patch] Steam stores unpatched");
  };
}
