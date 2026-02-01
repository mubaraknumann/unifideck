/**
 * useGameInfo hook
 *
 * Fetches and caches game information from the backend.
 * Implements 5-second TTL cache to balance freshness with performance.
 */

import { useState, useEffect } from "react";
import { call } from "@decky/api";

// Global cache for game info (5-second TTL for faster updates after installation)
const gameInfoCache = new Map<number, { info: any; timestamp: number }>();
const CACHE_TTL = 5000; // 5 seconds - reduced from 30s for faster button state updates

export interface GameInfo {
  game_id: string;
  title: string;
  store: string;
  is_installed: boolean;
  install_size?: number;
  download_size?: number;
  [key: string]: any;
}

/**
 * Hook to fetch and cache game information
 * @param appId - Steam app ID
 * @returns Game info object or null if not found/error
 */
export function useGameInfo(appId: number): GameInfo | null {
  const [gameInfo, setGameInfo] = useState<GameInfo | null>(null);

  useEffect(() => {
    // Check cache first
    const cached = gameInfoCache.get(appId);
    if (cached && Date.now() - cached.timestamp < CACHE_TTL) {
      setGameInfo(cached.info);
      return;
    }

    // Fetch from backend
    call<[number], any>("get_game_info", appId)
      .then((info) => {
        const processedInfo = info?.error ? null : info;
        setGameInfo(processedInfo);
        gameInfoCache.set(appId, {
          info: processedInfo,
          timestamp: Date.now(),
        });
      })
      .catch(() => setGameInfo(null));
  }, [appId]);

  return gameInfo;
}

/**
 * Invalidate cache entry for a specific app ID
 * Use this after installation/uninstallation to force fresh data
 */
export function invalidateGameInfoCache(appId: number): void {
  gameInfoCache.delete(appId);
}

/**
 * Clear entire game info cache
 * Use during plugin unload
 */
export function clearGameInfoCache(): void {
  gameInfoCache.clear();
}

/**
 * Refresh game info and update cache
 * Returns the updated info
 */
export async function refreshGameInfo(appId: number): Promise<GameInfo | null> {
  try {
    invalidateGameInfoCache(appId);
    const info = await call<[number], any>("get_game_info", appId);
    const processedInfo = info?.error ? null : info;

    if (processedInfo) {
      gameInfoCache.set(appId, {
        info: processedInfo,
        timestamp: Date.now(),
      });
    }

    return processedInfo;
  } catch {
    return null;
  }
}
