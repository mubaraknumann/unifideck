/**
 * Unifideck Tab Filters
 * 
 * Filters for creating custom library tabs that include
 * Steam, Epic, GOG, and Amazon games with proper store detection.
 */

import { getCachedRating, getCachedCompatByTitle, meetsGreatOnDeckCriteria } from './protondb';

// Filter types supported by Unifideck
export type FilterType = 'installed' | 'platform' | 'store' | 'deckCompat' | 'all' | 'nonSteam';

// Filter parameters for each filter type
export interface FilterParams {
    installed: { installed: boolean };
    platform: { platform: 'steam' | 'nonSteam' | 'all' };
    store: { store: 'steam' | 'epic' | 'gog' | 'amazon' | 'all' };
    deckCompat: {};  // No params needed - uses Native/Platinum/Verified only
    all: {};
    nonSteam: {};  // All non-Steam shortcuts except non-installed Unifideck
}

// A filter setting combines type with its params
export interface TabFilter<T extends FilterType = FilterType> {
    type: T;
    params: FilterParams[T];
}

// Non-Steam shortcut app_type value
const NON_STEAM_APP_TYPE = 1073741824;

// Steam Deck compatibility categories
const DECK_VERIFIED = 3;  // steam_deck_compat_category
const DECK_PLAYABLE = 2;

// Cache for Unifideck game info (store mapping, install status, and Steam appId for ProtonDB)
// Key is appId - we store BOTH signed and unsigned versions for lookup
export const unifideckGameCache: Map<number, {
    store: 'epic' | 'gog' | 'amazon';
    isInstalled: boolean;
    steamAppId?: number;  // Real Steam appId for ProtonDB lookups
}> = new Map();

/**
 * Updates the Unifideck game cache with store info
 * Stores both signed and unsigned versions of appId for reliable lookup
 * NOTE: This REPLACES the entire cache - use updateSingleGameStatus for individual updates
 */
export function updateUnifideckCache(games: Array<{
    appId: number;
    store: 'epic' | 'gog' | 'amazon';
    isInstalled: boolean;
    steamAppId?: number;
}>) {
    console.log(`[Unifideck] Updating game cache with ${games.length} games`);
    unifideckGameCache.clear();
    games.forEach(game => {
        const signedId = game.appId;
        // Convert signed to unsigned and vice versa for reliable lookup
        const unsignedId = signedId < 0 ? signedId + 0x100000000 : signedId;
        const altSignedId = signedId >= 0 && signedId > 0x7FFFFFFF ? signedId - 0x100000000 : signedId;

        const entry = {
            store: game.store,
            isInstalled: game.isInstalled,
            steamAppId: game.steamAppId
        };
        unifideckGameCache.set(signedId, entry);
        unifideckGameCache.set(unsignedId, entry);
        if (altSignedId !== signedId) {
            unifideckGameCache.set(altSignedId, entry);
        }
    });
    console.log(`[Unifideck] Cache now has ${unifideckGameCache.size} entries (${games.length} games x2 for signed/unsigned)`);
}

/**
 * Updates a SINGLE game's installation status in the cache
 * Does NOT clear other entries - safe for real-time UI updates
 */
export function updateSingleGameStatus(game: {
    appId: number;
    store: 'epic' | 'gog' | 'amazon';
    isInstalled: boolean;
}) {
    const signedId = game.appId;
    const unsignedId = signedId < 0 ? signedId + 0x100000000 : signedId;
    const altSignedId = signedId >= 0 && signedId > 0x7FFFFFFF ? signedId - 0x100000000 : signedId;

    // Check if entry exists and update it, or create new entry
    const existingEntry = unifideckGameCache.get(signedId);
    const entry = {
        store: game.store,
        isInstalled: game.isInstalled,
        steamAppId: existingEntry?.steamAppId
    };

    unifideckGameCache.set(signedId, entry);
    unifideckGameCache.set(unsignedId, entry);
    if (altSignedId !== signedId) {
        unifideckGameCache.set(altSignedId, entry);
    }
    console.log(`[Unifideck] Updated single game status: ${game.store}:${game.appId} installed=${game.isInstalled}`);
}

/**
 * Check if a game is a Unifideck-managed game
 */
export function isUnifideckGame(appId: number): boolean {
    return unifideckGameCache.has(appId);
}

/**
 * Gets the store for a given app
 * Returns null if unknown non-Steam shortcut (not in our cache)
 */
export function getStoreForApp(appId: number, appType: number): 'steam' | 'epic' | 'gog' | 'amazon' | null {
    // Check cache first (works for both signed and unsigned appId)
    const cached = unifideckGameCache.get(appId);
    if (cached) {
        return cached.store;
    }

    // If it's a non-Steam shortcut but not in our cache, return null (unknown)
    // This lets us filter it out from Epic/GOG/Steam tabs but keep in Non-Steam
    if (appType === NON_STEAM_APP_TYPE) {
        return null;  // Unknown non-Steam shortcut
    }

    return 'steam';  // Native Steam game
}

/**
 * Gets installed status for a game from our cache
 */
export function getInstalledStatus(appId: number, _appType: number, steamInstalledFlag: boolean): boolean {
    // For Unifideck games, use our cache
    const cached = unifideckGameCache.get(appId);
    if (cached) {
        return cached.isInstalled;
    }

    // For Steam games, use Steam's installed flag
    return steamInstalledFlag;
}

/**
 * Filter functions for each filter type
 */
export const filterFunctions: {
    [K in FilterType]: (params: FilterParams[K], app: SteamAppOverview) => boolean;
} = {
    // Show all Steam games AND Unifideck games (but not unrelated non-Steam shortcuts)
    all: (_params, app) => {
        // Native Steam games - always include
        if (app.app_type !== NON_STEAM_APP_TYPE) {
            return true;
        }
        // Non-Steam shortcuts - only include if it's a Unifideck game
        return isUnifideckGame(app.appid);
    },

    // Filter by installation status
    // Shows ALL installed games/shortcuts (Steam, Unifideck, and other non-Steam)
    installed: (params, app) => {
        const isInstalled = getInstalledStatus(app.appid, app.app_type, app.installed);
        return params.installed ? isInstalled : !isInstalled;
    },

    // Filter by platform (Steam vs non-Steam)
    platform: (params, app) => {
        if (params.platform === 'all') return true;
        if (params.platform === 'steam') {
            return app.app_type !== NON_STEAM_APP_TYPE;
        }
        return app.app_type === NON_STEAM_APP_TYPE;
    },

    // Filter by store (Steam, Epic, GOG, Amazon)
    store: (params, app) => {
        if (params.store === 'all') return true;

        const store = getStoreForApp(app.appid, app.app_type);

        // If store is null (unknown non-Steam shortcut), don't include in any store tab
        if (store === null) {
            return false;
        }

        return store === params.store;
    },

    // Filter by Steam Deck compatibility
    // Includes: Native, Platinum, or Steam Deck Verified
    deckCompat: (_params, app) => {
        // Steam Deck Verified - always pass (Steam games only)
        if (app.steam_deck_compat_category === DECK_VERIFIED) {
            return true;
        }

        // For Unifideck games (Epic/GOG/Amazon), use title-based compatibility lookup
        const cached = unifideckGameCache.get(app.appid);
        if (cached) {
            // Use app's display_name for title-based search
            const title = app.display_name || '';
            if (title) {
                const compat = getCachedCompatByTitle(title);
                return meetsGreatOnDeckCriteria(compat);
            }
            return false;
        }

        // For Steam games, use appId-based ProtonDB lookup
        const protonRating = getCachedRating(app.appid);
        if (protonRating) {
            if (protonRating === 'native' || protonRating === 'platinum') {
                return true;
            }
        }

        // No rating or doesn't meet criteria
        return false;
    },

    // Non-Steam tab: All non-Steam shortcuts EXCEPT non-installed Unifideck games
    nonSteam: (_params, app) => {
        // Only include non-Steam shortcuts
        if (app.app_type !== NON_STEAM_APP_TYPE) {
            return false;
        }

        // Check if it's a Unifideck game
        const cached = unifideckGameCache.get(app.appid);
        if (cached) {
            // It's a Unifideck game - only show if installed
            return cached.isInstalled;
        }

        // Not a Unifideck game - show all other non-Steam shortcuts
        return true;
    }
};

/**
 * Get Steam's hidden collection containing hidden app IDs
 * This is cached per-call since the collection is dynamic
 */
function getHiddenAppIds(): Set<number> {
    try {
        const collectionStore = (window as any).collectionStore;
        const hiddenCollection = collectionStore?.GetCollection?.('hidden');
        if (hiddenCollection?.allApps) {
            return new Set(hiddenCollection.allApps.map((app: any) => app.appid));
        }
    } catch (e) {
        console.error('[Unifideck] Error getting hidden collection:', e);
    }
    return new Set();
}

/**
 * Check if a game is hidden by the user
 * Uses Steam's 'hidden' collection which is updated dynamically
 */
export function isGameHidden(appId: number): boolean {
    const hiddenIds = getHiddenAppIds();
    return hiddenIds.has(appId);
}

/**
 * Runs a filter against an app
 */
export function runFilter(filter: TabFilter, app: SteamAppOverview): boolean {
    const filterFn = filterFunctions[filter.type];
    if (!filterFn) return true;
    return filterFn(filter.params as any, app);
}

/**
 * Runs multiple filters against an app (AND logic)
 * Also excludes hidden games from all tabs/collections
 */
export function runFilters(filters: TabFilter[], app: SteamAppOverview): boolean {
    // Always exclude hidden games from all tabs
    // Uses Steam's 'hidden' collection which updates dynamically
    if (isGameHidden(app.appid)) {
        return false;
    }
    return filters.every(filter => runFilter(filter, app));
}

// Type declaration for SteamAppOverview (simplified)
declare global {
    interface SteamAppOverview {
        appid: number;
        display_name: string;
        app_type: number;
        installed: boolean;
        steam_deck_compat_category?: number;
        // Steam sets this to false for hidden games
        // Updated dynamically when user hides/unhides games
        visible_in_game_list?: boolean;
    }
}
