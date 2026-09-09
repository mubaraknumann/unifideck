/**
 * Playtime / activity tracking DTOs.
 *
 * Mirrors the backend `services/playtime/` package. Field
 * names use the wire format (snake_case) directly.
 */
export interface PlaySession {
  id: number;
  game_id: number;
  started_at: string;
  ended_at: string | null;
  duration_secs: number | null;
  end_reason: string;
  title: string;
  store: string;
  steam_app_id: number | null;
  proton_tool: string | null;
  is_manual: number;
  session_note: string | null;
}

/**
 * Per-game playtime stats : total minutes, last session date,
 * sessions count. Used by GameInfoPanel and DownloadsTab.
 */
export interface GameStats {
  game_id: number;
  title: string;
  store: string;
  steam_app_id: number | null;
  total_secs: number;
  total_sessions: number;
  avg_session_secs: number;
  min_session_secs: number | null;
  max_session_secs: number;
  first_played_at: string | null;
  last_played_at: string | null;
  current_streak_days: number;
  longest_streak_days: number;
}

/**
 * One bucket of the daily playtime histogram, used by the
 * QuickAccessPanel chart. Date is ISO YYYY-MM-DD.
 */
export interface DailyTotal {
  date: string;
  total_secs: number;
  session_count: number;
  games_played: number;
}

/**
 * Wire shape of `get_playtime`.
 *
 * `total_seconds` is the local-only total; `store_total_secs` is the store's
 * authoritative cross-device total (GOG/Epic), `null` until first synced —
 * prefer it for display when present (it's the superset of local + other
 * devices).
 *
 * `game_id` / `title` are optional because they were only populated by the
 * bulk `get_all_playtimes` route, deleted in the audit §1.2 pass (the library
 * view sources bulk playtime from Steam's own `GetPlaytime` instead). The
 * per-game route does not set them.
 */
export interface PlaytimeEntry {
  store: string;
  game_id?: string;
  title?: string;
  total_seconds: number;
  store_total_secs: number | null;
  session_count: number;
  last_played: string | null;
  current_streak: number;
  longest_streak: number;
  is_active: boolean;
}

/**
 * Aggregated playtime across all games and stores, with the
 * top-N favourites surfaced for the dashboard widget.
 */
export interface OverallStats {
  total_secs: number;
  total_sessions: number;
  total_games_played: number;
  most_active_hour: number | null;
  most_active_day: string | null;
  average_daily_secs: number;
  this_week_secs: number;
  last_week_secs: number;
}
