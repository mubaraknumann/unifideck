/**
 * Type definitions for Steam Client APIs
 * These are based on observed behavior and may change with Steam updates
 */

export interface SteamApp {
  appid: number;
  display_name: string;
  sort_as: string;
  installed: boolean;
  is_shortcuts_app: boolean;  // True for non-Steam games
  BIsShortcut(): boolean;
  size_on_disk: string;
  minutes_playtime_forever: string;
  minutes_playtime_last_two_weeks: string;
  rt_last_time_played: number;
  store_category: number[];
  app_type: number;
  canonicalAppType: number;
  local_per_client_data?: {
    is_hidden?: boolean;
  };
}

export interface SteamAppOverview extends SteamApp {
  icon_hash: string;
  review_score: number;
  review_percentage: number;
  GameID(): string;
  GetCapsuleImageURL(): string;
  GetHeaderImageURL(): string;
  GetLibraryImageURL(): string;
}

export interface SteamCollection {
  id: string;
  name: string;
  added_timestamp: number;
  bIsDynamic: boolean;
  visibleApps: number[];
}

// Global Steam Client API interfaces
declare global {
  interface Window {
    SteamClient?: {
      Apps?: {
        GetOwnedApps(): SteamApp[];
        GetNonSteamApps(): SteamApp[];
        GetAppOverview(appId: number): SteamAppOverview | null;
      };
      library?: {
        GetCollections(): SteamCollection[];
      };
    };
    collectionStore?: {
      userCollections?: Map<string, SteamCollection>;
      deckDesktopApps?: SteamCollection;
      localGamesCollection?: SteamCollection;
    };
    // Note: appStore is defined by @decky/ui globals
  }
}

export type StoreType = 'steam' | 'epic' | 'gog' | 'amazon' | 'unknown';

export interface UnifideckGame {
  appId: number;
  title: string;
  store: StoreType;
  isInstalled: boolean;
  isShortcut: boolean;
  tags?: string[];
  deckVerified?: 'verified' | 'playable' | 'unsupported' | 'unknown';
  lastPlayed?: number;
  playtimeMinutes?: number;
}
