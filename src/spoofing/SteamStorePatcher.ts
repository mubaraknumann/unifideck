import { call } from "@decky/api";

// Cache for shortcut -> real Steam App ID mappings
let steamAppIdMappings: Record<number, number> = {};

// Cache for pre-fetched store data (synchronous access)
let storeDataCache: Record<number, any> = {};
let appDetailsCache: Record<number, any> = {};

// Track which overviews we've already patched to avoid duplicate work
const patchedOverviews = new Set<number>();

// Reference to unpatch function for m_mapApps patching
let unpatchMapApps: (() => void) | null = null;

/**
 * Load the Steam App ID mappings from backend.
 * Call this during plugin initialization.
 */
export async function loadSteamAppIdMappings(): Promise<void> {
  try {
    console.log("[Unifideck Store Patch] Loading Steam App ID mappings...");
    const result = await call<
      [],
      { success: boolean; mappings: Record<number, number> }
    >("get_real_steam_appid_mappings");

    console.log("[Unifideck Store Patch] Backend response:", result?.success, "mappings count:", result?.mappings ? Object.keys(result.mappings).length : 0);

    if (result?.success && result.mappings) {
      steamAppIdMappings = result.mappings;
      const count = Object.keys(steamAppIdMappings).length;
      console.log(
        `[Unifideck Store Patch] Loaded ${count} Steam App ID mappings`,
      );
      // Log first few mappings for debugging
      const sampleKeys = Object.keys(steamAppIdMappings).slice(0, 3);
      console.log("[Unifideck Store Patch] Sample mappings:", sampleKeys.map(k => `${k} -> ${steamAppIdMappings[k as any]}`));

      // Load cached metadata from backend (pre-fetched during sync)
      if (count > 0) {
        await loadSteamMetadataCache();
      }
    } else {
      console.log("[Unifideck Store Patch] No mappings found or load failed");
    }
  } catch (error) {
    console.error("[Unifideck Store Patch] Failed to load mappings:", error);
  }
}

/**
 * Load pre-cached Steam metadata from backend.
 * This metadata was fetched during sync operations.
 */
async function loadSteamMetadataCache(): Promise<void> {
  try {
    const result = await call<
      [],
      { success: boolean; metadata: Record<number, any> }
    >("get_steam_metadata_cache");

    if (result?.success && result.metadata) {
      // Build cache objects from pre-fetched metadata
      let successCount = 0;
      let errorCount = 0;
      for (const [steamAppIdStr, gameData] of Object.entries(result.metadata)) {
        try {
          const steamAppId = parseInt(steamAppIdStr);
          storeDataCache[steamAppId] = buildSteamAppOverview(steamAppId, gameData);
          appDetailsCache[steamAppId] = buildAppDetails(steamAppId, gameData);
          successCount++;
        } catch (entryError) {
          errorCount++;
          // Only log first few errors to avoid spam
          if (errorCount <= 3) {
            console.warn(`[Unifideck Store Patch] Error building cache for ${steamAppIdStr}:`, entryError);
          }
        }
      }
      console.log(
        `[Unifideck Store Patch] Loaded ${successCount} cached metadata entries (${errorCount} errors)`,
      );
    } else {
      console.log(
        "[Unifideck Store Patch] No metadata cache found - run Sync Libraries to fetch Steam metadata",
      );
    }
  } catch (error) {
    console.error("[Unifideck Store Patch] Failed to load metadata cache:", error);
  }
}

/**
 * Convert signed shortcut ID to unsigned 32-bit for m_mapApps lookup.
 * Steam uses unsigned IDs internally (e.g., 2890841912) but we track signed (e.g., -1404125384).
 */
function toUnsignedAppId(signedAppId: number): number {
  if (signedAppId < 0) {
    return signedAppId + 0x100000000; // Convert to unsigned 32-bit
  }
  return signedAppId;
}

/**
 * Force re-inject metadata for a specific shortcut.
 * Call this when navigating to game details to ensure metadata is fresh.
 */
export function forceInjectMetadataForShortcut(shortcutAppId: number): boolean {
  const appStore = (window as any).appStore;
  if (!appStore?.m_mapApps) return false;

  // Convert to unsigned for m_mapApps lookup (Steam uses unsigned IDs)
  const unsignedAppId = toUnsignedAppId(shortcutAppId);

  // Clear from patched set to allow re-injection (using unsigned)
  patchedOverviews.delete(unsignedAppId);

  const overview = appStore.m_mapApps.get(unsignedAppId);
  if (overview) {
    const result = injectMetadataIntoOverview(overview);
    console.log(`[Unifideck] Force re-injected metadata for ${shortcutAppId} (unsigned: ${unsignedAppId}): ${result}`);
    return result;
  } else {
    console.log(`[Unifideck] No overview found in m_mapApps for ${unsignedAppId}`);
  }
  return false;
}

/**
 * Inject a single game into Steam's appinfo.vdf when user opens its details view.
 * Only call this for Unifideck shortcuts (check steamAppIdMappings first).
 * Fire-and-forget: doesn't block UI, injection happens in background.
 *
 * EXPORTED: Call this from patchGameDetailsRoute when navigating to a shortcut.
 */
export async function injectGameToAppinfo(shortcutAppId: number): Promise<void> {
  // Only inject if this is a Unifideck shortcut
  if (!steamAppIdMappings[shortcutAppId]) {
    return; // Not a Unifideck shortcut, skip
  }

  // Force inject metadata into the appOverview (immediate, synchronous)
  forceInjectMetadataForShortcut(shortcutAppId);

  // Also inject into appinfo.vdf for persistence (async, fire and forget)
  try {
    const result = await call<[number], { success: boolean }>(
      "inject_game_to_appinfo",
      shortcutAppId,
    );

    if (result?.success) {
      console.log(
        `[Unifideck Store Patch] Injected game ${shortcutAppId} to appinfo.vdf`,
      );
    }
  } catch (error) {
    console.error(
      `[Unifideck Store Patch] Failed to inject game ${shortcutAppId}:`,
      error,
    );
  }
}

/**
 * Safely extract IDs from categories/genres (handles both array and dict formats).
 * Some Steam API responses have arrays like [{id: 1, description: "..."}]
 * Others have dicts like {category_1: 1, category_2: 1}
 */
function extractIds(data: any): number[] {
  if (!data) return [];
  if (Array.isArray(data)) {
    return data.map((item: any) => item.id).filter((id: any) => id != null);
  }
  if (typeof data === "object") {
    // Handle dict format like {category_1: 1, category_2: 1}
    return Object.keys(data)
      .filter((k) => k.startsWith("category_") || k.startsWith("genre_"))
      .map((k) => parseInt(k.split("_")[1]))
      .filter((n) => !isNaN(n));
  }
  return [];
}

/**
 * Safely extract languages (handles both string and dict formats).
 * String: "English, French, German"
 * Dict: {english: 1, french: 1, german: 1}
 */
function extractLanguages(data: any): Array<{ strLanguageName: string }> {
  if (!data) return [];
  if (typeof data === "string") {
    return data.split(",").map((l: string) => ({ strLanguageName: l.trim() }));
  }
  if (typeof data === "object" && !Array.isArray(data)) {
    // Handle dict format
    return Object.keys(data).map((lang) => ({
      strLanguageName: lang.charAt(0).toUpperCase() + lang.slice(1),
    }));
  }
  return [];
}

/**
 * Convert unsigned shortcut ID to signed for steamAppIdMappings lookup.
 */
function toSignedAppId(unsignedAppId: number): number {
  if (unsignedAppId > 0x7FFFFFFF) {
    return unsignedAppId - 0x100000000; // Convert to signed 32-bit
  }
  return unsignedAppId;
}

/**
 * Inject metadata directly into a shortcut's appOverview object.
 * This is similar to how NSL patches playtime - we modify the actual object properties.
 */
function injectMetadataIntoOverview(appOverview: any): boolean {
  try {
    if (!appOverview) return false;

    const appId = typeof appOverview.appid === "function"
      ? appOverview.appid()
      : appOverview.appid;

    if (!appId) return false;

    // Check if already patched (use unsigned for tracking)
    if (patchedOverviews.has(appId)) return false;

    // Convert to signed for steamAppIdMappings lookup
    // Steam uses unsigned in m_mapApps but our mappings use signed
    const signedAppId = toSignedAppId(appId);
    let realSteamAppId = steamAppIdMappings[signedAppId];
    if (!realSteamAppId) {
      // Also try with the raw appId in case mappings are mixed
      realSteamAppId = steamAppIdMappings[appId];
      if (!realSteamAppId) return false;
    }

    // Get cached metadata
    const metadata = appDetailsCache[realSteamAppId];
    if (!metadata) {
      console.log(`[Unifideck] No cached metadata for shortcut ${appId} -> Steam ${realSteamAppId}`);
      return false;
    }

    console.log(`[Unifideck] Injecting metadata into overview for ${appId} (${metadata.strDisplayName || "unknown"})`);

    // Inject metadata fields into appOverview
    // These are the fields Steam's UI might read for the GAME INFO tab

    // Developer/Publisher (some UIs read these directly from appOverview)
    if (metadata.strDeveloperName) {
      appOverview.developer = metadata.strDeveloperName;
      appOverview.strDeveloperName = metadata.strDeveloperName;
    }

    if (metadata.associations?.rgPublishers?.[0]?.strName) {
      appOverview.publisher = metadata.associations.rgPublishers[0].strName;
      appOverview.strPublisherName = metadata.associations.rgPublishers[0].strName;
    }

    // Description
    if (metadata.strDescription) {
      appOverview.short_description = metadata.strDescription;
      appOverview.strShortDescription = metadata.strDescription;
    }

    // Release date (Unix timestamp)
    if (metadata.rtReleaseDate) {
      appOverview.rt_original_release_date = metadata.rtReleaseDate;
      appOverview.rt_steam_release_date = metadata.rtReleaseDate;
    }

    // Metacritic score
    if (metadata.__web_api_data?.metacritic?.score) {
      appOverview.metacritic_score = metadata.__web_api_data.metacritic.score;
    }

    // Controller support
    if (metadata.eSteamInputControllerMask !== undefined) {
      appOverview.controller_support = metadata.eSteamInputControllerMask;
    }

    // Store the full details for potential later use
    appOverview.__unifideck_details = metadata;

    // Mark as patched
    patchedOverviews.add(appId);

    // Trigger UI update if available
    if (typeof appOverview.TriggerChange === "function") {
      appOverview.TriggerChange();
    }

    return true;
  } catch (error) {
    console.error("[Unifideck] Failed to inject metadata into overview:", error);
    return false;
  }
}

/**
 * Scan all shortcuts in m_mapApps and inject metadata.
 */
function injectMetadataToAllShortcuts(): void {
  const appStore = (window as any).appStore;
  if (!appStore?.m_mapApps) return;

  let count = 0;
  let found = 0;
  const shortcutIds = Object.keys(steamAppIdMappings).map(Number);

  console.log(`[Unifideck] Scanning ${shortcutIds.length} shortcuts for metadata injection...`);

  for (const signedShortcutId of shortcutIds) {
    // Convert signed to unsigned for m_mapApps lookup
    const unsignedShortcutId = toUnsignedAppId(signedShortcutId);
    const overview = appStore.m_mapApps.get(unsignedShortcutId);

    if (overview) {
      found++;
      if (injectMetadataIntoOverview(overview)) {
        count++;
      }
    }
  }

  console.log(`[Unifideck] Found ${found} shortcuts in m_mapApps, injected metadata into ${count}`);
}

/**
 * Patch appStore.m_mapApps to intercept when apps are added/updated.
 * Similar to NSL's approach for playtime tracking.
 */
function patchMapApps(): () => void {
  const appStore = (window as any).appStore;
  const appInfoStore = (window as any).appInfoStore;

  if (!appStore?.m_mapApps) {
    console.error("[Unifideck] appStore.m_mapApps not available");
    return () => {};
  }

  // Store original methods
  const originalSet = appStore.m_mapApps.set;
  const originalOnAppOverviewChange = appInfoStore?.OnAppOverviewChange;

  // Patch m_mapApps.set to inject metadata when shortcuts are added
  appStore.m_mapApps.set = function (appId: number, appOverview: any) {
    const result = originalSet.call(this, appId, appOverview);

    // Try to inject metadata if this is a Unifideck shortcut
    // Wrap in try-catch to prevent breaking Steam if injection fails
    try {
      // appId from m_mapApps is unsigned, convert to signed for mapping lookup
      const signedAppId = toSignedAppId(appId);
      if (steamAppIdMappings[signedAppId] || steamAppIdMappings[appId]) {
        injectMetadataIntoOverview(appOverview);
      }
    } catch (error) {
      console.error("[Unifideck] Error in m_mapApps.set patch:", error);
    }

    return result;
  };

  // Mark that we've patched set
  (appStore.m_mapApps as any)._unifideck_originalSet = originalSet;

  // Also patch OnAppOverviewChange if available
  if (appInfoStore && originalOnAppOverviewChange) {
    appInfoStore.OnAppOverviewChange = function (apps: any[]) {
      // Inject metadata for each app in the change set
      // Wrap in try-catch to prevent breaking Steam if injection fails
      try {
        for (const app of apps || []) {
          const id = typeof app?.appid === "function" ? app.appid() : app?.appid;
          if (id) {
            // Convert to signed for mapping lookup
            const signedId = toSignedAppId(id);
            if (steamAppIdMappings[signedId] || steamAppIdMappings[id]) {
              const overview = appStore.m_mapApps.get(id) || app;
              injectMetadataIntoOverview(overview);
            }
          }
        }
      } catch (error) {
        console.error("[Unifideck] Error in OnAppOverviewChange patch:", error);
      }
      return originalOnAppOverviewChange.call(this, apps);
    };
    (appInfoStore as any)._unifideck_originalOnChange = originalOnAppOverviewChange;
  }

  console.log("[Unifideck] m_mapApps patched for metadata injection");

  // Note: We don't do bulk injection at startup to avoid crashes
  // Metadata will be injected on-demand when:
  // 1. User navigates to game details (via injectGameToAppinfo)
  // 2. Steam adds shortcuts to m_mapApps (via patched set method)
  // 3. Steam updates app overviews (via patched OnAppOverviewChange)

  // Return unpatch function
  return () => {
    if ((appStore.m_mapApps as any)._unifideck_originalSet) {
      appStore.m_mapApps.set = (appStore.m_mapApps as any)._unifideck_originalSet;
      delete (appStore.m_mapApps as any)._unifideck_originalSet;
    }
    if (appInfoStore && (appInfoStore as any)._unifideck_originalOnChange) {
      appInfoStore.OnAppOverviewChange = (appInfoStore as any)._unifideck_originalOnChange;
      delete (appInfoStore as any)._unifideck_originalOnChange;
    }
    patchedOverviews.clear();
    console.log("[Unifideck] m_mapApps unpatched");
  };
}

/**
 * Build a SteamAppOverview-compatible object from web API data.
 */
function buildSteamAppOverview(steamAppId: number, gameData: any): any {
  return {
    appid: steamAppId,
    display_name: gameData.name,
    app_type: 1, // Game
    visible_in_game_list: true,
    sort_as: gameData.name?.toLowerCase() || "",

    // Release date
    rt_original_release_date: gameData.release_date?.date
      ? Math.floor(new Date(gameData.release_date.date).getTime() / 1000)
      : 0,
    rt_steam_release_date: gameData.release_date?.date
      ? Math.floor(new Date(gameData.release_date.date).getTime() / 1000)
      : 0,

    // Controller support
    controller_support:
      gameData.controller_support === "full"
        ? 2
        : gameData.controller_support === "partial"
          ? 1
          : 0,

    // Steam Deck compatibility (not in web API, default to unknown)
    steam_deck_compat_category: 0,

    // Store info (handle both array and dict formats)
    store_tag: extractIds(gameData.genres),
    store_category: extractIds(gameData.categories),
    metacritic_score: gameData.metacritic?.score || 0,

    // Review scores
    review_score_with_bombs: gameData.recommendations?.total
      ? Math.round(
          (gameData.recommendations.total /
            (gameData.recommendations.total + 1)) *
            100,
        )
      : 0,
    review_percentage_with_bombs: 0,

    // Images
    icon_hash: "",
    header_filename: gameData.header_image || "",
    library_capsule_filename: gameData.capsule_image || "",

    // Methods (stubs)
    BIsShortcut: () => false,
    BIsModOrShortcut: () => false,
    GameID: () => steamAppId.toString(),

    // Flag to identify web API data
    __from_web_api: true,
    __web_api_data: gameData,
  };
}

/**
 * Build an AppDetails-compatible object from web API data.
 */
function buildAppDetails(steamAppId: number, gameData: any): any {
  const developers = gameData.developers || [];
  const publishers = gameData.publishers || [];

  return {
    unAppID: steamAppId,
    strDisplayName: gameData.name,

    // Developer/Publisher info
    strDeveloperName: developers[0] || "",
    strDeveloperURL: "",
    strHomepageURL: gameData.website || "",

    // Descriptions (for GAME INFO tab)
    strDescription: gameData.short_description || "",
    strFullDescription: gameData.detailed_description || "",

    // Associations (developers, publishers)
    associations: {
      rgDevelopers: developers.map((d: string) => ({ strName: d, strURL: "" })),
      rgPublishers: publishers.map((p: string) => ({ strName: p, strURL: "" })),
      rgFranchises: [],
    },

    // Release date
    rtReleaseDate: gameData.release_date?.date
      ? Math.floor(new Date(gameData.release_date.date).getTime() / 1000)
      : 0,

    // Platforms
    vecPlatforms: [
      gameData.platforms?.windows && "windows",
      gameData.platforms?.mac && "osx",
      gameData.platforms?.linux && "linux",
    ].filter(Boolean),

    // Languages (handle both string and dict formats)
    vecLanguages: extractLanguages(gameData.supported_languages),

    // Categories/features (handle both array and dict formats)
    bCloudAvailable: extractIds(gameData.categories).includes(23),
    bCloudEnabledForApp: extractIds(gameData.categories).includes(23),

    // Achievements
    achievements: {
      nAchieved: 0,
      nTotal: gameData.achievements?.total || 0,
      vecAchievedHidden: [],
      vecHighlight: [],
      vecUnachieved: [],
    },

    // Controller support
    eSteamInputControllerMask:
      gameData.controller_support === "full"
        ? 2
        : gameData.controller_support === "partial"
          ? 1
          : 0,

    // DLC
    vecDLC:
      gameData.dlc?.map((id: number) => ({
        appid: id,
        strName: "",
        bInstalled: false,
      })) || [],

    // Screenshots count
    nScreenshots: gameData.screenshots?.length || 0,

    // Size
    lDiskSpaceRequiredBytes: 0,

    // Deck compatibility (not available in web API)
    vecDeckCompatTestResults: [],

    // Flag
    __from_web_api: true,
    __web_api_data: gameData,
  };
}

/**
 * Get cached overview data synchronously.
 */
function getCachedOverview(steamAppId: number): any | null {
  return storeDataCache[steamAppId] || null;
}

/**
 * Get cached details data synchronously.
 */
function getCachedDetails(steamAppId: number): any | null {
  return appDetailsCache[steamAppId] || null;
}

/**
 * Patch Steam's global data stores to return real Steam game data.
 * IMPORTANT: All returns are synchronous using pre-cached data.
 */
export function patchSteamStores(): () => void {
  console.log("[Unifideck Store Patch] Starting store patching...");
  console.log("[Unifideck Store Patch] steamAppIdMappings count:", Object.keys(steamAppIdMappings).length);
  console.log("[Unifideck Store Patch] storeDataCache count:", Object.keys(storeDataCache).length);

  const appStore = (window as any).appStore;
  const appDetailsStore = (window as any).appDetailsStore;

  console.log("[Unifideck Store Patch] appStore available:", !!appStore);
  console.log("[Unifideck Store Patch] appDetailsStore available:", !!appDetailsStore);

  if (!appStore) {
    console.error("[Unifideck Store Patch] appStore not available");
    return () => {};
  }

  if (!appDetailsStore) {
    console.error("[Unifideck Store Patch] appDetailsStore not available");
    return () => {};
  }

  console.log("[Unifideck Store Patch] Both stores found, proceeding with patching...");

  // Store original methods
  const origGetAppOverview = appStore.GetAppOverviewByAppID.bind(appStore);
  const origGetAppDetails = appDetailsStore.GetAppDetails.bind(appDetailsStore);
  const origGetAppData = appDetailsStore.GetAppData.bind(appDetailsStore);

  // Patch GetAppOverviewByAppID (used by most UI components)
  appStore.GetAppOverviewByAppID = function (appId: number) {
    // Debug: Log every call to track if this method is used for shortcuts
    const isNegative = appId < 0;
    if (isNegative) {
      console.log(`[Unifideck Store Patch] GetAppOverviewByAppID called with SHORTCUT appId: ${appId}`);
    }

    const realSteamAppId = steamAppIdMappings[appId];

    if (!realSteamAppId) {
      // Not a Unifideck shortcut, return original
      if (isNegative) {
        console.log(`[Unifideck Store Patch] Shortcut ${appId} NOT in mappings (${Object.keys(steamAppIdMappings).length} mappings loaded)`);
      }
      return origGetAppOverview(appId);
    }

    console.log(`[Unifideck Store Patch] MATCH! Shortcut ${appId} -> Steam ${realSteamAppId}`);

    // This is a Unifideck shortcut - trigger appinfo.vdf injection (fire and forget)
    // This ensures Steam's native UI can read the game's metadata
    injectGameToAppinfo(appId);

    // Try owned game first (fast)
    const ownedData = origGetAppOverview(realSteamAppId);
    if (ownedData) {
      console.log(
        `[Unifideck Store Patch] GetAppOverview: ${appId} -> owned ${realSteamAppId}`,
      );
      return ownedData;
    }

    // Return cached web API data (synchronous)
    const cached = getCachedOverview(realSteamAppId);
    if (cached) {
      console.log(
        `[Unifideck Store Patch] GetAppOverview: ${appId} -> cached ${realSteamAppId}`,
      );
      return cached;
    }

    console.log(
      `[Unifideck Store Patch] GetAppOverview: ${appId} -> no data for ${realSteamAppId}`,
    );
    return origGetAppOverview(appId);
  };

  // Patch GetAppDetails (used by GAME INFO tab)
  appDetailsStore.GetAppDetails = function (appId: number) {
    // Debug: Log every call for shortcuts
    const isNegative = appId < 0;
    if (isNegative) {
      console.log(`[Unifideck Store Patch] GetAppDetails called with SHORTCUT appId: ${appId}`);
    }

    const realSteamAppId = steamAppIdMappings[appId];

    if (!realSteamAppId) {
      if (isNegative) {
        console.log(`[Unifideck Store Patch] GetAppDetails: Shortcut ${appId} NOT in mappings`);
      }
      return origGetAppDetails(appId);
    }

    console.log(`[Unifideck Store Patch] GetAppDetails MATCH! ${appId} -> ${realSteamAppId}`);

    // Try owned game first
    const ownedDetails = origGetAppDetails(realSteamAppId);
    if (ownedDetails) {
      console.log(
        `[Unifideck Store Patch] GetAppDetails: ${appId} -> owned ${realSteamAppId}`,
      );
      return ownedDetails;
    }

    // Return cached web API data (synchronous)
    const cached = getCachedDetails(realSteamAppId);
    if (cached) {
      console.log(
        `[Unifideck Store Patch] GetAppDetails: ${appId} -> cached ${realSteamAppId}`,
      );
      return cached;
    }

    console.log(
      `[Unifideck Store Patch] GetAppDetails: ${appId} -> no data for ${realSteamAppId}`,
    );
    return origGetAppDetails(appId);
  };

  // Patch GetAppData (combined overview + details)
  appDetailsStore.GetAppData = function (appId: number) {
    const realSteamAppId = steamAppIdMappings[appId];

    if (!realSteamAppId) {
      return origGetAppData(appId);
    }

    // Try owned game first
    const ownedData = origGetAppData(realSteamAppId);
    if (ownedData) {
      console.log(
        `[Unifideck Store Patch] GetAppData: ${appId} -> owned ${realSteamAppId}`,
      );
      return ownedData;
    }

    // Return cached data
    const cachedOverview = getCachedOverview(realSteamAppId);
    const cachedDetails = getCachedDetails(realSteamAppId);
    if (cachedOverview || cachedDetails) {
      console.log(
        `[Unifideck Store Patch] GetAppData: ${appId} -> cached ${realSteamAppId}`,
      );
      return {
        overview: cachedOverview,
        details: cachedDetails,
      };
    }

    return origGetAppData(appId);
  };

  console.log("[Unifideck Store Patch] Steam stores patched successfully");

  // DISABLED: m_mapApps patching was causing Steam to hang/crash
  // Instead, we only inject metadata on-demand when user opens game details
  // This is done via injectGameToAppinfo() called from patchGameDetailsRoute

  // Logging for debugging
  console.log(`[Unifideck Store Patch] Loaded ${Object.keys(steamAppIdMappings).length} shortcut mappings`);
  console.log(`[Unifideck Store Patch] Cached ${Object.keys(appDetailsCache).length} metadata entries`);
  console.log(`[Unifideck Store Patch] Metadata will be injected on-demand when opening game details`);

  // Return unpatch function
  return () => {
    appStore.GetAppOverviewByAppID = origGetAppOverview;
    appDetailsStore.GetAppDetails = origGetAppDetails;
    appDetailsStore.GetAppData = origGetAppData;
    storeDataCache = {};
    appDetailsCache = {};

    // m_mapApps patching disabled, nothing to unpatch

    console.log("[Unifideck Store Patch] Steam stores unpatched");
  };
}

