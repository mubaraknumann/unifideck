/**
 * ProtonDB & Steam Deck Integration
 *
 * Compatibility data is now loaded from backend cache (compat_cache.json)
 * which is populated during sync/force sync by the Python backend.
 *
 * This removes the need for frontend API calls and provides instant lookups.
 */

import { call } from "@decky/api";

// ProtonDB rating tiers
export type ProtonDBTier =
  | "platinum"
  | "gold"
  | "silver"
  | "bronze"
  | "borked"
  | "pending"
  | "native";

// Steam Deck compatibility categories
export type DeckVerifiedStatus =
  | "verified"
  | "playable"
  | "unsupported"
  | "unknown";

// Combined compatibility info
export interface GameCompatInfo {
  tier: ProtonDBTier | null;
  deckVerified: DeckVerifiedStatus;
  steamAppId: number | null;
}

// Cache for combined game compatibility (loaded from backend)
interface CompatCacheEntry extends GameCompatInfo {
  timestamp: number;
}

// Memory cache for quick lookups (keyed by game title, normalized)
const compatCache = new Map<string, CompatCacheEntry>();

// Memory cache for appId-based lookups (for Steam games)
const protonDBCache = new Map<
  number,
  { tier: ProtonDBTier; timestamp: number }
>();

// Flag to track if cache has been loaded from backend
let cacheLoadedFromBackend = false;

/**
 * Normalize title for cache key
 */
function normalizeTitle(title: string): string {
  return title.toLowerCase().trim();
}

/**
 * Load compatibility cache from backend (Python compat_cache.json)
 * Called once during TabManager initialization
 */
export async function loadCompatCacheFromBackend(): Promise<void> {
  if (cacheLoadedFromBackend) {
    console.log("[Unifideck] Compat cache already loaded from backend");
    return;
  }

  try {
    console.log("[Unifideck] Loading compat cache from backend...");
    const backendCache = await call<[], Record<string, CompatCacheEntry>>(
      "get_compat_cache",
    );

    if (backendCache && typeof backendCache === "object") {
      // Populate memory cache from backend data
      for (const [key, entry] of Object.entries(backendCache)) {
        if (entry) {
          compatCache.set(key, {
            tier: (entry.tier as ProtonDBTier) || null,
            deckVerified:
              (entry.deckVerified as DeckVerifiedStatus) || "unknown",
            steamAppId: entry.steamAppId || null,
            timestamp: entry.timestamp || Date.now(),
          });

          // Also populate appId cache for future lookups
          if (entry.steamAppId && entry.tier) {
            protonDBCache.set(entry.steamAppId, {
              tier: entry.tier as ProtonDBTier,
              timestamp: entry.timestamp || Date.now(),
            });
          }
        }
      }

      cacheLoadedFromBackend = true;
      console.log(
        `[Unifideck] Loaded ${compatCache.size} entries from backend compat cache`,
      );
    } else {
      console.log("[Unifideck] No compat cache data returned from backend");
    }
  } catch (error) {
    console.error(
      "[Unifideck] Error loading compat cache from backend:",
      error,
    );
  }
}

/**
 * Get cached ProtonDB rating (synchronous - for filtering)
 * Used for native Steam games
 */
export function getCachedRating(appId: number): ProtonDBTier | null {
  const cached = protonDBCache.get(appId);
  if (cached) {
    return cached.tier;
  }
  return null;
}

/**
 * Get cached compatibility info by title (synchronous - for filtering)
 * Used for Epic/GOG/Amazon games
 */
export function getCachedCompatByTitle(title: string): GameCompatInfo | null {
  const key = normalizeTitle(title);
  const cached = compatCache.get(key);
  if (cached) {
    return {
      tier: cached.tier,
      deckVerified: cached.deckVerified,
      steamAppId: cached.steamAppId,
    };
  }
  return null;
}

/**
 * Check if a rating meets the "Great on Deck" criteria
 */
export function meetsGreatOnDeckCriteria(
  compat: GameCompatInfo | null,
): boolean {
  if (!compat) return false;

  // Steam Deck Verified OR Playable passes
  if (compat.deckVerified === "verified" || compat.deckVerified === "playable")
    return true;

  // ProtonDB Native or Platinum passes (regardless of Deck status)
  if (compat.tier === "native" || compat.tier === "platinum") return true;

  // Gold only passes if ALSO Steam Deck Verified/Playable (already handled above)
  // So Gold-only games without Deck status will fail here

  return false;
}

/**
 * Check if compat cache has been loaded
 */
export function isCompatCacheLoaded(): boolean {
  return cacheLoadedFromBackend;
}

/**
 * Get the number of cached entries
 */
export function getCompatCacheSize(): number {
  return compatCache.size;
}
