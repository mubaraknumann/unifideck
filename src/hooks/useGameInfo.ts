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
 *  - A failed fetch is never cached; a mounted hook retries it with
 *    backoff and reports `status: "unavailable"` if no answer comes
 */
import { useCallback, useEffect, useState } from "react";
import { useRPC } from "../api/useRPC";
import { rpcRoutes } from "../api/rpc-routes";
import type { Game, GameTag, StoreId, XboxPcInfo } from "../types/api";

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
    browser_url: browserUrlOf(r),
    xbox_pc: xboxPcOf(r),
  };
}

/** ``metadata.xbox_pc`` off a raw game row, when it is an object with a
 *  string ``state``. The row's tag says it is an Xbox PC title; these are
 *  its install facts (plan, package, size). Exported for tests. */
export function xboxPcOf(r: Record<string, unknown>): XboxPcInfo | undefined {
  const meta = r.metadata;
  if (!meta || typeof meta !== "object") return undefined;
  const info = (meta as Record<string, unknown>).xbox_pc;
  if (!info || typeof info !== "object") return undefined;
  const state = (info as Record<string, unknown>).state;
  return typeof state === "string" ? (info as XboxPcInfo) : undefined;
}

/** ``metadata.browser_url`` off a raw game row, when it is a string. */
function browserUrlOf(r: Record<string, unknown>): string | undefined {
  const meta = r.metadata;
  if (meta && typeof meta === "object") {
    const url = (meta as Record<string, unknown>).browser_url;
    if (typeof url === "string" && url) return url;
  }
  return undefined;
}

/** Cache entry. */
interface CacheEntry {
  data: Game | null;
  ts: number;
  inflight: Promise<Game | null> | null;
}

const CACHE_TTL = 5000;
const cache = new Map<number, CacheEntry>();

/**
 * Backoff between attempts after a failed ``get_game_info``. The last
 * value repeats until {@link RETRY_BUDGET_MS} is spent.
 *
 * A failure here is almost always the backend not being up yet: Decky
 * serves RPCs while ``_main`` is still booting, so a page open across a
 * plugin restart or update can ask before the library is loaded. Asking
 * once and caching the failure is how a restart used to leave the
 * App-Details page on the loading skeleton until the user navigated away
 * and back.
 */
export const RETRY_DELAYS_MS: readonly number[] = [500, 1000, 2000, 4000, 8000];

/** Total time a mounted hook keeps retrying a failing fetch. */
export const RETRY_BUDGET_MS = 30_000;

/**
 * How long a page may wait with no answer before it stops showing the
 * loading skeleton and reports ``unavailable``. Also covers a call that
 * never returns at all, which no retry loop can see.
 */
export const UNAVAILABLE_AFTER_MS = 10_000;

/** Knobs for {@link fetchWithRetry}; injectable so tests need no timers. */
export interface RetryOptions {
  delaysMs: readonly number[];
  budgetMs: number;
  sleep: (ms: number) => Promise<void>;
  now: () => number;
  isCancelled: () => boolean;
}

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Run ``attempt`` until it resolves, backing off between failures.
 *
 * A resolved value, including ``null`` ("not a Unifideck game"), is final.
 * Only a rejection is retried. Rethrows the last error once the budget is
 * spent or the caller is gone.
 */
export async function fetchWithRetry<T>(
  attempt: () => Promise<T>,
  opts: RetryOptions,
): Promise<T> {
  const start = opts.now();
  for (let i = 0; ; i++) {
    try {
      return await attempt();
    } catch (err) {
      const delay = opts.delaysMs[Math.min(i, opts.delaysMs.length - 1)] ?? 0;
      if (opts.isCancelled() || opts.now() - start + delay > opts.budgetMs) {
        throw err;
      }
      await opts.sleep(delay);
      if (opts.isCancelled()) throw err;
    }
  }
}

/**
 * Fetch one appId through the module cache.
 *
 * Concurrent callers share one in-flight request unless ``force`` is set.
 * A success is cached; a failure is NOT. The entry goes back to whatever
 * good data it held before (or away entirely), so the next caller asks
 * the backend again instead of being served a cached ``null`` that reads
 * as "not ours" for the rest of the TTL.
 */
export function fetchGameInfoCached(
  appId: number,
  fetch: (id: number) => Promise<Game | null>,
  force: boolean,
): Promise<Game | null> {
  const prev = cache.get(appId);
  if (!force && prev?.inflight) return prev.inflight;

  const promise = fetch(appId).then(
    (data) => {
      cache.set(appId, { data, ts: Date.now(), inflight: null });
      return data;
    },
    (err: unknown) => {
      if (cache.get(appId)?.inflight === promise) {
        if (prev?.data != null) {
          cache.set(appId, { data: prev.data, ts: prev.ts, inflight: null });
        } else {
          cache.delete(appId);
        }
      }
      throw err;
    },
  );
  cache.set(appId, {
    data: prev?.data ?? null,
    ts: prev?.ts ?? 0,
    inflight: promise,
  });
  return promise;
}

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
 * Where a {@link useGameInfo} lookup stands.
 *
 *  - ``loading``     — no answer yet.
 *  - ``ready``       — ``data`` holds the game.
 *  - ``missing``     — the backend answered: not a Unifideck game.
 *  - ``unavailable`` — no answer within {@link UNAVAILABLE_AFTER_MS}, or
 *    every retry failed. The backend is down or still booting; callers
 *    should hand the page back to Steam rather than wait on it. Retries
 *    continue in the background and a late answer flips this to ``ready``.
 */
export type GameInfoStatus = "loading" | "ready" | "missing" | "unavailable";

/**
 * Aggregated game info returned by {@link useGameInfo} —
 * description, scores, artwork URLs, playtime fragments —
 * with loading and error flags propagated through.
 */
export interface UseGameInfoResult {
  data: Game | null;
  loading: boolean;
  error: Error | null;
  status: GameInfoStatus;
  refresh: () => Promise<void>;
}

interface GameInfoState {
  data: Game | null;
  loading: boolean;
  error: Error | null;
  status: GameInfoStatus;
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
  const [state, setState] = useState<GameInfoState>(() => {
    if (appId == null) {
      return { data: null, loading: false, error: null, status: "missing" };
    }
    const cached = cache.get(appId);
    const data = cached?.data ?? null;
    return {
      data,
      loading: data == null,
      error: null,
      status: data != null ? "ready" : "loading",
    };
  });

  const load = useCallback(
    async (force: boolean, isCancelled: () => boolean): Promise<void> => {
      if (appId == null) {
        setState({
          data: null,
          loading: false,
          error: null,
          status: "missing",
        });
        return;
      }

      const cached = cache.get(appId);
      if (
        !force &&
        cached &&
        !cached.inflight &&
        Date.now() - cached.ts < CACHE_TTL
      ) {
        setState({
          data: cached.data,
          loading: false,
          error: null,
          status: cached.data != null ? "ready" : "missing",
        });
        return;
      }

      // Stale-while-revalidate : if we have ANY cached data,
      // keep showing it while the background refresh runs. A page
      // already handed back to Steam stays handed back until an
      // answer arrives, so a retry never brings the skeleton back.
      setState((s) => {
        const data = s.data ?? cached?.data ?? null;
        return {
          data,
          loading: data == null,
          error: null,
          status:
            data != null
              ? "ready"
              : s.status === "unavailable"
              ? "unavailable"
              : "loading",
        };
      });

      try {
        const data = await fetchWithRetry(
          () => fetchGameInfoCached(appId, fetch, force),
          {
            delaysMs: RETRY_DELAYS_MS,
            budgetMs: RETRY_BUDGET_MS,
            sleep,
            now: Date.now,
            isCancelled,
          },
        );
        if (isCancelled()) return;
        setState({
          data,
          loading: false,
          error: null,
          status: data != null ? "ready" : "missing",
        });
      } catch (err) {
        if (isCancelled()) return;
        setState((s) => ({
          data: s.data,
          loading: false,
          error: err as Error,
          status: s.data != null ? "ready" : "unavailable",
        }));
      }
    },
    [appId, fetch],
  );

  useEffect(() => {
    let cancelled = false;
    void load(false, () => cancelled);
    // A call that never returns keeps the retry loop parked on one
    // await, so the deadline is a separate timer. It only ever moves
    // a page that still has nothing to show.
    const deadline = setTimeout(() => {
      if (cancelled) return;
      setState((s) =>
        s.data == null && s.status === "loading"
          ? { ...s, loading: false, status: "unavailable" }
          : s,
      );
    }, UNAVAILABLE_AFTER_MS);
    return () => {
      cancelled = true;
      clearTimeout(deadline);
    };
  }, [load]);

  // Re-fetch when something invalidates this appId (download
  // complete / uninstall / cancel). Without this, the mounted
  // hook keeps its stale state — the Play section stays on the
  // wrong button until the page is reopened.
  useEffect(() => {
    if (appId == null) return;
    let cancelled = false;
    const unsubscribe = subscribeGameInfo(appId, () => {
      void load(true, () => cancelled);
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [appId, load]);

  const refresh = useCallback(() => load(true, () => false), [load]);

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
