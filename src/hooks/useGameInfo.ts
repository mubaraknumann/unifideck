/**
 * useGameInfo — per-appId info fetch with TTL cache.
 *
 * Replaces the global `gameInfoCache` Map that lived in the
 * old `index.tsx` (and was passed via setter callbacks all
 * over the place). Each component that needs game info just
 * calls `useGameInfo(appId)` and gets reactive {data, loading,
 * error, refresh}. The hook shares a module-level cache so
 * concurrent consumers of the same appId don't re-fetch.
 *
 * Cache invariants :
 *  - TTL = 5000ms (matches old behaviour)
 *  - Same appId across components = single fetch in flight
 *  - `refresh()` always re-fetches (bypasses cache)
 */
import { useCallback, useEffect, useState } from "react";
import { useRPC } from "../api/useRPC";
import { rpcRoutes } from "../api/rpc-routes";
import type { Game, GameTag, StoreId } from "../types/api";

/**
 * Adapt the raw ``get_game_info`` RPC response into our
 * frontend ``Game`` shape.
 *
 * Backend's :class:`Game` dataclass uses ``installed`` /
 * ``store_game_id`` / ``exe_path``; the frontend Game interface
 * (older shape, predates the unified-types refactor) expects
 * ``is_installed`` / ``id`` / ``executable``. Without this
 * adapter, every consumer of ``useGameInfo`` sees
 * ``game.is_installed === undefined`` (falsy → "not installed")
 * and ``game.id === undefined`` (so download-queue matching by
 * ``game.id === download.game_id`` always misses), which is why
 * the Play section stays on Install even mid-download.
 */
function adaptGame(raw: unknown): Game | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const storeGameId = String(r.store_game_id ?? r.id ?? "");
  if (!storeGameId) return null;
  return {
    id: storeGameId,
    store_game_id: storeGameId,
    title: String(r.title ?? ""),
    store: (r.store ?? "unknown") as StoreId,
    is_installed: Boolean(r.installed ?? r.is_installed),
    install_path:
      typeof r.install_path === "string" ? r.install_path : undefined,
    executable:
      typeof r.exe_path === "string"
        ? r.exe_path
        : typeof r.executable === "string"
        ? r.executable
        : undefined,
    app_id: typeof r.app_id === "number" ? r.app_id : undefined,
    size_bytes: typeof r.size_bytes === "number" ? r.size_bytes : undefined,
    cover_image: typeof r.cover_image === "string" ? r.cover_image : undefined,
    // Backend serialises store tags as ``tags`` (e.g. ``["xcloud"]``
    // for Xbox Cloud games); expose them as ``store_tags`` so the
    // play-section logic can branch on cloud-streaming titles.
    store_tags: Array.isArray(r.store_tags)
      ? (r.store_tags as GameTag[])
      : Array.isArray(r.tags)
      ? (r.tags as GameTag[])
      : undefined,
  };
}

/** Cache entry. */
interface CacheEntry {
  data: Game | null;
  ts: number;
  inflight: Promise<Game | null> | null;
}

const CACHE_TTL = 5000;
const cache = new Map<number, CacheEntry>();

// Mounted-hook subscribers, keyed by appId. `invalidateGameInfo`
// notifies these so a live `useGameInfo` refetches immediately
// (e.g. when a download completes and `is_installed` flips) —
// clearing the module cache alone doesn't re-run a mounted hook,
// which is why the Play section used to stay on "Install" until
// the user reopened the page.
const subscribers = new Map<number, Set<() => void>>();

function subscribeGameInfo(appId: number, fn: () => void): () => void {
  let set = subscribers.get(appId);
  if (!set) {
    set = new Set();
    subscribers.set(appId, set);
  }
  set.add(fn);
  return () => {
    set?.delete(fn);
    if (set && set.size === 0) subscribers.delete(appId);
  };
}

function notifyGameInfo(appId: number): void {
  subscribers.get(appId)?.forEach((fn) => fn());
}

/**
 * Aggregated game info returned by {@link useGameInfo} —
 * description, scores, artwork URLs, playtime fragments —
 * with loading and error flags propagated through.
 */
export interface UseGameInfoResult {
  data: Game | null;
  loading: boolean;
  error: Error | null;
  refresh: () => Promise<void>;
}

/**
 * Hook that aggregates all metadata Unifideck has on
 * a given game : description, scores, artwork,
 * playtime stats. Uses `useRPCQuery` under the hood
 * with a sensible cache TTL so opening the info panel
 * is instant on revisits.
 *
 * @param appId — Steam shortcut app-id.
 * @returns aggregated info + loading/error flags.
 */
export function useGameInfo(appId: number | null): UseGameInfoResult {
  // `get_game_info(app_id)` is the route for "look up by Steam
  // shortcut appid" — the appId boundary has no store/game-id pair.
  // (A store-keyed `get_game_metadata` route once existed for that
  // shape; it had no caller and was deleted in the audit §1.2 pass.
  // `get_game_metadata_display` is the appid-keyed survivor.)
  // We receive the raw backend dict (snake_case, ``installed``
  // not ``is_installed``, etc.) and adapt it via ``adaptGame``
  // below; declaring the RPC return as ``unknown`` keeps the
  // type system honest about the wire shape.
  const fetchRaw = useRPC<[number], unknown>(rpcRoutes.getGameInfo);
  const fetch = useCallback(
    async (id: number): Promise<Game | null> => adaptGame(await fetchRaw(id)),
    [fetchRaw],
  );
  // Lazy priming : if the module-level cache has ANY entry for
  // this appId (fresh OR stale), seed the initial state with it
  // so consumers paint immediately. Stale data still triggers a
  // background refresh below.
  const [state, setState] = useState<{
    data: Game | null;
    loading: boolean;
    error: Error | null;
  }>(() => {
    if (appId == null) return { data: null, loading: false, error: null };
    const cached = cache.get(appId);
    return {
      data: cached?.data ?? null,
      loading: cached?.data == null,
      error: null,
    };
  });

  const load = useCallback(
    async (force: boolean): Promise<void> => {
      if (appId == null) {
        setState({ data: null, loading: false, error: null });
        return;
      }

      const cached = cache.get(appId);
      if (!force && cached && Date.now() - cached.ts < CACHE_TTL) {
        setState({ data: cached.data, loading: false, error: null });
        return;
      }

      // De-duplicate concurrent in-flight fetches
      if (cached?.inflight && !force) {
        const data = await cached.inflight;
        setState({ data, loading: false, error: null });
        return;
      }

      // Stale-while-revalidate : if we have ANY cached data,
      // keep showing it while the background refresh runs.
      setState((s) => ({
        data: s.data ?? cached?.data ?? null,
        loading: s.data == null && cached?.data == null,
        error: null,
      }));
      const promise = fetch(appId).then(
        (data) => {
          cache.set(appId, { data, ts: Date.now(), inflight: null });
          return data;
        },
        (err) => {
          cache.set(appId, { data: null, ts: Date.now(), inflight: null });
          throw err;
        },
      );

      cache.set(appId, {
        data: cached?.data ?? null,
        ts: cached?.ts ?? 0,
        inflight: promise,
      });

      try {
        const data = await promise;
        setState({ data, loading: false, error: null });
      } catch (err) {
        setState({ data: null, loading: false, error: err as Error });
      }
    },
    [appId, fetch],
  );

  useEffect(() => {
    void load(false);
  }, [load]);

  // Re-fetch when something invalidates this appId (download
  // complete / uninstall / cancel). Without this, the mounted
  // hook keeps its stale state — the Play section stays on the
  // wrong button until the page is reopened.
  useEffect(() => {
    if (appId == null) return;
    return subscribeGameInfo(appId, () => {
      void load(true);
    });
  }, [appId, load]);

  const refresh = useCallback(() => load(true), [load]);

  return { ...state, refresh };
}

/** Test/dev helper — clear the module-level cache. Not
 *  exposed via the barrel; imported only by vitest specs. */
export function _clearGameInfoCache(): void {
  cache.clear();
}

/** Drop the cache entry for one appId so the next render
 *  re-fetches. Called after destructive actions (uninstall,
 *  cancel) where `is_installed` flips. Mirrors the legacy
 *  `gameInfoCache.delete(appId)` semantics — also drops the
 *  signed/unsigned variants since Steam shortcuts may be
 *  represented either way in the cache. */
export function invalidateGameInfo(appId: number): void {
  const signed = appId > 0x7fffffff ? appId - 0x100000000 : appId;
  const unsigned = appId < 0 ? appId + 0x100000000 : appId;
  // Clear the cache for every representation, then notify mounted
  // hooks so they refetch. The caller may pass either the signed
  // (backend Game.app_id) or unsigned (Steam shortcut) form, while
  // the mounted hook is keyed on whichever the page handed it — so
  // we fan out to both.
  for (const id of new Set([appId, signed, unsigned])) {
    cache.delete(id);
    notifyGameInfo(id);
  }
}
