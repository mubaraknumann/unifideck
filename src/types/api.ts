/**
 * Backend RPC contract — TypeScript mirror of `core/types/`.
 *
 * Every dataclass exposed via `to_dict()` on the Python side
 * has its TS interface here. Field names use the wire format
 * (snake_case) so JSON parsing is a no-op cast — no runtime
 * adapter, no field rename pass.
 *
 * If a field is added on the backend dataclass, it MUST be
 * added here in the same PR that lands the backend change.
 * The contract is enforced by reviewers, not by tooling
 * (TypeScript can't see Python).
 */
import type { CompatTrack } from "../lib/steam-bridge/compat-packed";

/** One verification test-result row in the compatibility details
 *  modal. ``passed === true`` renders a green checkmark; ``false``
 *  renders a yellow warning.
 *
 *  Carries Valve's own ``loc_token``, localised at render time through
 *  the Steam client (see ``lib/compat-tokens.ts``). ``text`` appears
 *  only on cache entries written before that rework and holds
 *  pre-resolved English. */
export interface CompatTestResult {
  token?: string;
  text?: string;
  passed: boolean;
}

/** One device's rating for a game. */
export interface CompatTrackInfo {
  /** ``0`` unknown, ``1`` unsupported, ``2`` playable, ``3`` verified.
   *  For the ``steamos`` track ``2`` means "SteamOS Compatible" and
   *  ``3`` is never used. */
  category: 0 | 1 | 2 | 3;
  status: string;
  test_results: CompatTestResult[];
}

/** Rich display metadata for the game info panel — sourced from
 *  Steam Store appdetails (preferred), UnifiDB, and Metacritic
 *  (fallback). Returned by ``get_game_metadata_display``. Kept
 *  separate from {@link Game} so install-state and
 *  display-metadata can be cached and refreshed independently. */
export interface GameMetadata {
  /** Real Steam App ID when the shortcut was resolved to a Steam
   *  store entry, ``0`` otherwise. Gates the steam:// nav buttons. */
  steam_app_id: number;
  /** True when ``steam_app_id`` corresponds to a real Steam Store
   *  page (validated against the cached appdetails payload). */
  has_steam_store_page: boolean;
  store: StoreId;
  /** Third-party store landing URL — used when no Steam page exists. */
  store_url: string;
  title: string;
  developer: string;
  publisher: string;
  release_date: string;
  metacritic: number | null;
  description: string;
  /** Which rating track describes the device this is running on —
   *  resolved by the backend from DMI, so the UI never guesses. */
  compat_device: CompatTrack;
  /** Every device's rating, keyed by track. Shipping all of them costs
   *  nothing at one game and leaves room to show cross-device ratings
   *  without another RPC. */
  compat: Record<CompatTrack, CompatTrackInfo>;
  genres: string[];
  homepage_url?: string;
  /** Whether THIS store's copy of the game has native cloud saves.
   *  ``null``/absent = unknown (no enriched entry), and the UI stays quiet
   *  rather than claiming an absence. Known before the game is installed, so
   *  it can inform which storefront's copy to download. */
  cloud_saves?: boolean | null;
}

/** Universal `Game` representation aggregated from any store. */
export interface Game {
  id: string;
  store_game_id: string;
  title: string;
  store: StoreId;
  /** Adapter-normalised install flag (set by ``adaptGame`` on the
   *  app-details path). NOTE: raw rows straight off
   *  ``get_all_unifideck_games`` do NOT carry this — they carry
   *  ``installed`` (the wire field, below). Read ``installed ?? is_installed``
   *  when consuming un-adapted rows. */
  is_installed: boolean;
  /** Raw wire field from ``asdict(Game)`` (backend ``Game.installed``).
   *  Present on un-adapted RPC rows; ``adaptGame`` folds it into
   *  ``is_installed``. */
  installed?: boolean;
  cover_image?: string;
  install_path?: string;
  executable?: string;
  app_id?: number;
  steam_app_id?: number;
  ownership_type?: OwnershipType;
  store_tags?: GameTag[];
  size_bytes?: number;
}

/** One achievement (definition + this user's unlock status). */
export interface Achievement {
  key: string;
  name: string;
  description: string;
  image_unlocked: string;
  image_locked: string;
  hidden: boolean;
  unlocked: boolean;
  /** Epoch seconds the achievement was unlocked, or null if still locked. */
  unlocked_at: number | null;
  rarity?: number | null;
}

/** A game's achievements + summary (from `get_game_achievements`). */
export interface GameAchievements {
  store: StoreId;
  game_id: string;
  total: number;
  unlocked: number;
  percent: number;
  achievements: Achievement[];
}

/** Last play session's unlock summary (from `get_last_session_achievements`). */
export interface LastSessionAchievements {
  names: string[];
  unlocked: number;
  total: number;
  /** Epoch seconds the session ended. */
  at: number;
}

/** Common wrapper for every RPC method's response. */
export interface Result {
  success: boolean;
  error?: string;
}

/** Auth start/complete/logout response. */
export interface AuthResult extends Result {
  url?: string;
  token?: string;
  store: StoreId;
}

/** Install completion response. */
export interface InstallResult extends Result {
  install_path?: string;
  game_id: string;
  size_mb?: number;
  store: StoreId;
}

/** Sync run summary. */
export interface SyncResult extends Result {
  games: Game[];
  store: StoreId;
  count: number;
  duration_ms: number;
}

/** Download progress snapshot. */
export interface DownloadResult extends Result {
  progress: number;
  game_id: string;
  store: StoreId;
  queued: boolean;
}

/**
 * One entry of the `get_store_infos` payload.
 *
 * This interface used to declare `icon` and `auth_status`, **neither of
 * which the backend has ever sent** — `StoreInfo` carries `icon_asset`, and
 * auth state comes from the separate `check_store_status` route keyed by
 * `store_id`. Four fields that *were* sent went undeclared. A type that
 * matches an unread payload is still a lie, so this now mirrors the wire
 * shape exactly (audit register item 26).
 *
 * The `supports_*` / `has_*` flags are derived server-side from
 * `core/store_capabilities.py` and are the reason the frontend no longer
 * hand-maintains its own per-store lists — the audit found sixteen of those
 * with a single machine-checked pair between them. Read a capability off
 * here (see `useStoreCapability`) rather than writing a new `Set([...])`.
 */
export interface StoreInfo {
  name: StoreId;
  display_name: string;
  auth_method: string;
  icon_asset: string;
  supports_install: boolean;
  available: boolean;
  client_runs_in_prefix: boolean;
  supports_achievements: boolean;
  supports_cloud_saves: boolean;
  has_language_picker: boolean;
  has_browser_storefront: boolean;
}

/** Capability keys carried by {@link StoreInfo}. */
export type StoreCapability =
  | "supports_install"
  | "supports_achievements"
  | "supports_cloud_saves"
  | "has_language_picker"
  | "has_browser_storefront"
  | "client_runs_in_prefix";

/**
 * Discriminator for which store a Game/Auth/Download
 * payload comes from.
 *
 * The set is closed on purpose : every backend route
 * accepting a store argument validates against this
 * union and rejects anything else. Adding a 6th store
 * therefore requires a coordinated change in both
 * `core/types/events.py` (StoreEnum) and this file.
 */
export type StoreId =
  | "steam"
  | "epic"
  | "gog"
  | "amazon"
  | "microsoft"
  | "ubisoft"
  | "battlenet"
  | "gamevault";

/**
 * Per-store availability + auth state, returned by
 * `check_store_status` RPC. The frontend uses it to
 * decide whether to show a Connect button, a Sync
 * button, or a re-auth prompt.
 *
 *  - `unauthenticated` : no token present
 *  - `authenticated`   : token valid, ready to sync
 *  - `error`           : token rejected by the store API
 *  - `unavailable`     : store CLI / Wine prefix missing
 */
export type StoreStatus = "connected" | "disconnected" | "expired" | "error";

/**
 * How the user owns a given title. Discriminates
 * subscription games (xCloud, Game Pass) from
 * purchased ones, which matters for badge display
 * and uninstall confirmation copy.
 */
export type OwnershipType = "owned" | "subscription" | "trial";

/**
 * Tag attached to a Game by its store. Drives the
 * coloured pill rendered in `GameInfoMetadata`. Tags
 * are additive : a game can carry several at once
 * (e.g. `dlc` + `early-access`).
 */
export type GameTag =
  | "demo"
  | "addon"
  | "dlc"
  | "preorder"
  | "early_access"
  // Xbox Cloud Gaming title — streamed in a browser, never installed.
  // Drives the "Play on Cloud" play-section variant.
  | "xcloud";
