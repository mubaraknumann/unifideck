/**
 * ProtonDB & Steam Deck Integration
 * 
 * Two-step lookup for Epic/GOG games:
 * 1. Steam Store Search API (title → Steam AppID)
 * 2. ProtonDB API (appId → tier rating)
 * 
 * Also fetches Steam Deck Verified status.
 */

import { fetchNoCors } from '@decky/api';

// ProtonDB rating tiers
export type ProtonDBTier = 'platinum' | 'gold' | 'silver' | 'bronze' | 'borked' | 'pending' | 'native';

// Steam Deck compatibility categories
export type DeckVerifiedStatus = 'verified' | 'playable' | 'unsupported' | 'unknown';

// Combined compatibility info
export interface GameCompatInfo {
    tier: ProtonDBTier | null;
    deckVerified: DeckVerifiedStatus;
    steamAppId: number | null;
}

// Cache for combined game compatibility with TTL
interface CompatCacheEntry extends GameCompatInfo {
    timestamp: number;
}

// Memory cache for quick lookups (keyed by game title, normalized)
const compatCache = new Map<string, CompatCacheEntry>();
const CACHE_TTL = 24 * 60 * 60 * 1000; // 24 hours

// Memory cache for appId-based lookups (for Steam games)
const protonDBCache = new Map<number, { tier: ProtonDBTier; timestamp: number }>();

/**
 * Normalize title for cache key
 */
function normalizeTitle(title: string): string {
    return title.toLowerCase().trim();
}

/**
 * Search Steam Store for a game by title, returns Steam AppID
 */
async function searchSteamStore(title: string): Promise<{ appId: number; name: string } | null> {
    try {
        const encoded = encodeURIComponent(title);
        const res = await fetchNoCors(
            `https://store.steampowered.com/api/storesearch/?term=${encoded}&cc=US`,
            { method: 'GET' }
        );

        if (res.status === 200) {
            const data = await res.json();
            const items = data?.items;
            if (Array.isArray(items) && items.length > 0) {
                // Try to find best match by name similarity
                const normalizedSearch = normalizeTitle(title);
                const bestMatch = items.find((item: any) =>
                    normalizeTitle(item.name || '') === normalizedSearch
                ) || items[0];  // Fall back to first result

                return {
                    appId: bestMatch.id,
                    name: bestMatch.name
                };
            }
        }
    } catch (error) {
        console.log('[Unifideck] Steam Store search error:', title, error);
    }
    return null;
}

/**
 * Fetch ProtonDB rating for a Steam AppID
 */
async function fetchProtonDBRating(appId: number): Promise<ProtonDBTier | null> {
    try {
        const res = await fetchNoCors(
            `https://www.protondb.com/api/v1/reports/summaries/${appId}.json`,
            { method: 'GET' }
        );

        if (res.status === 200) {
            const data = await res.json();
            return data?.tier as ProtonDBTier || null;
        }
    } catch (error) {
        // 404 is normal for games not in ProtonDB
        if (!(error instanceof Error && error.message.includes('404'))) {
            console.log('[Unifideck] ProtonDB fetch error for app', appId, error);
        }
    }
    return null;
}

/**
 * Fetch Steam Deck verified status for a Steam AppID
 * Uses Steam's deck verification API
 */
async function fetchDeckVerifiedStatus(appId: number): Promise<DeckVerifiedStatus> {
    try {
        // Steam's deck compatibility API 
        const res = await fetchNoCors(
            `https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport?nAppID=${appId}`,
            { method: 'GET' }
        );

        if (res.status === 200) {
            const data = await res.json();
            // Steam returns category: 1=unknown, 2=unsupported, 3=playable, 4=verified
            const category = data?.results?.resolved_category;
            switch (category) {
                case 4: return 'verified';
                case 3: return 'playable';
                case 2: return 'unsupported';
                default: return 'unknown';
            }
        }
    } catch (error) {
        // Silently fail - many games don't have deck data
    }
    return 'unknown';
}

/**
 * Get ProtonDB rating for a Steam app (by appId)
 * Used for native Steam games
 */
export async function getProtonDBRating(appId: number): Promise<ProtonDBTier | null> {
    // Check memory cache
    const cached = protonDBCache.get(appId);
    if (cached && (Date.now() - cached.timestamp) < CACHE_TTL) {
        return cached.tier;
    }

    const tier = await fetchProtonDBRating(appId);
    if (tier) {
        protonDBCache.set(appId, { tier, timestamp: Date.now() });
    }
    return tier;
}

/**
 * Get cached ProtonDB rating (synchronous - for filtering)
 */
export function getCachedRating(appId: number): ProtonDBTier | null {
    const cached = protonDBCache.get(appId);
    if (cached && (Date.now() - cached.timestamp) < CACHE_TTL) {
        return cached.tier;
    }
    return null;
}

/**
 * Get full compatibility info for a game by title (for Epic/GOG games)
 * Two-step: Steam Store search → ProtonDB + Deck Verified
 */
export async function getCompatByTitle(title: string): Promise<GameCompatInfo> {
    const key = normalizeTitle(title);

    // Check cache
    const cached = compatCache.get(key);
    if (cached && (Date.now() - cached.timestamp) < CACHE_TTL) {
        return { tier: cached.tier, deckVerified: cached.deckVerified, steamAppId: cached.steamAppId };
    }

    // Step 1: Search Steam Store for AppID
    const searchResult = await searchSteamStore(title);
    if (!searchResult) {
        // Game not found on Steam
        const result: GameCompatInfo = { tier: null, deckVerified: 'unknown', steamAppId: null };
        compatCache.set(key, { ...result, timestamp: Date.now() });
        return result;
    }

    const { appId } = searchResult;

    // Step 2: Fetch ProtonDB rating and Deck Verified status in parallel
    const [tier, deckVerified] = await Promise.all([
        fetchProtonDBRating(appId),
        fetchDeckVerifiedStatus(appId)
    ]);

    const result: GameCompatInfo = {
        tier,
        deckVerified,
        steamAppId: appId
    };

    // Cache result
    compatCache.set(key, { ...result, timestamp: Date.now() });

    // Also cache in appId cache for future lookups
    if (tier) {
        protonDBCache.set(appId, { tier, timestamp: Date.now() });
    }

    console.log(`[Unifideck] Compat: "${title}" -> AppID ${appId}, tier=${tier}, deck=${deckVerified}`);
    return result;
}

/**
 * Get cached compatibility info by title (synchronous - for filtering)
 */
export function getCachedCompatByTitle(title: string): GameCompatInfo | null {
    const key = normalizeTitle(title);
    const cached = compatCache.get(key);
    if (cached && (Date.now() - cached.timestamp) < CACHE_TTL) {
        return { tier: cached.tier, deckVerified: cached.deckVerified, steamAppId: cached.steamAppId };
    }
    return null;
}

/**
 * Pre-fetch compatibility info for a list of game titles
 * Runs in parallel with concurrency limit for speed
 */
export async function prefetchCompatByTitles(titles: string[]): Promise<void> {
    console.log(`[Unifideck] Pre-fetching compatibility for ${titles.length} games...`);

    // Much faster: 10 concurrent lookups, minimal delay
    const batchSize = 10;  // 10 concurrent API calls
    const delayMs = 50;   // 200ms between batches (plenty for rate limiting)

    let processed = 0;
    let successful = 0;

    for (let i = 0; i < titles.length; i += batchSize) {
        const batch = titles.slice(i, i + batchSize);
        const results = await Promise.all(batch.map(title => getCompatByTitle(title)));

        processed += batch.length;
        successful += results.filter(r => r.tier !== null || r.deckVerified !== 'unknown').length;

        // Log progress every 50 games or at the end
        if (processed % 50 === 0 || processed === titles.length) {
            console.log(`[Unifideck] Compat prefetch: ${processed}/${titles.length} (${successful} found)`);
        }

        // Small delay between batches to be nice to APIs
        if (i + batchSize < titles.length) {
            await new Promise(resolve => setTimeout(resolve, delayMs));
        }
    }

    console.log(`[Unifideck] Compat prefetch complete: ${titles.length} games, ${successful} with ratings`);
}

/**
 * Check if a rating meets the "Great on Deck" criteria
 */
export function meetsGreatOnDeckCriteria(compat: GameCompatInfo | null): boolean {
    if (!compat) return false;

    // Steam Deck Verified always passes
    if (compat.deckVerified === 'verified') return true;

    // ProtonDB Native or Platinum passes (regardless of Deck status)
    if (compat.tier === 'native' || compat.tier === 'platinum') return true;

    // Gold only passes if ALSO Steam Deck Verified (already handled above)
    // So Gold-only games without Deck Verified will fail here

    return false;
}
