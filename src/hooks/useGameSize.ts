/**
 * useGameSize — lazy, non-blocking "Space Required" / "Installed
 * Size" fetch.
 *
 * The size lookup lives in its own RPC (`get_game_size_bytes`)
 * rather than in `get_game_info`, because resolving a download
 * size shells out to `legendary info` / `gogdl` (subprocess /
 * network) and can take seconds. `usePlaySection` + the game-info
 * panel both gate on `get_game_info`, so doing the size work there
 * stalled the whole custom UI behind Steam's native section. This
 * hook fetches the size separately, in an effect, exactly like
 * `MetaInline` already does for Last Played — the row renders
 * immediately and the size fills in a moment later.
 *
 * The cache is keyed by `"<appId>:<installed>"`, NOT `appId` alone:
 * the backend returns the *download* size while not-installed and
 * the *on-disk* size once installed, so a game that finishes
 * installing shows the right number rather than the stale pre-install
 * one. The module-level cache also de-dupes the fetch so the
 * play-section `MetaInline` and the info-panel size cell share one
 * round-trip.
 *
 * That key used to be the ONLY invalidation, on the reasoning that the
 * install-state transition is a natural cache miss. It is not enough.
 * A wrapper store (Ubisoft, Battle.net) once reported "installed" the
 * moment its Wine prefix was created, so the prefix-only measurement
 * landed under the same `<appId>:1` key the finished install would
 * read — and with no other invalidation, that wrong number was served
 * for the rest of the Steam session. Sizes are now forgotten
 * explicitly via `lib/game-size-cache`, which also covers the cases the
 * key never could: a game growing on update or shrinking when DLC goes.
 */
import { useEffect, useState } from "react";
import { call } from "@decky/api";
import { unwrapRpcEnvelope } from "../api/useRPC";
import { rpcRoutes } from "../api/rpc-routes";
import {
  onGameSizeInvalidated,
  registerGameSizeCache,
} from "../lib/game-size-cache";

const cache = new Map<string, number>();
const inflight = new Map<string, Promise<number>>();

function cacheKey(appId: number, installed: boolean): string {
  return `${appId}:${installed ? 1 : 0}`;
}

// Drop both install states for the app, and any fetch already in the air —
// an in-flight promise resolves into `cache`, so leaving it would write the
// stale value straight back after the invalidation.
registerGameSizeCache((appId) => {
  const prefix = `${appId}:`;
  for (const key of [...cache.keys()]) {
    if (key.startsWith(prefix)) cache.delete(key);
  }
  for (const key of [...inflight.keys()]) {
    if (key.startsWith(prefix)) inflight.delete(key);
  }
});

async function fetchSize(appId: number, key: string): Promise<number> {
  const cached = cache.get(key);
  if (cached != null) return cached;
  const existing = inflight.get(key);
  if (existing) return existing;

  const promise = (async () => {
    const raw = await call<[number], unknown>(
      rpcRoutes.getGameSizeBytes,
      appId,
    );
    const bytes = unwrapRpcEnvelope<number>(raw, {
      route: rpcRoutes.getGameSizeBytes,
      throwing: false,
    });
    const value = typeof bytes === "number" && bytes > 0 ? bytes : 0;
    cache.set(key, value);
    return value;
  })().finally(() => {
    inflight.delete(key);
  });

  inflight.set(key, promise);
  return promise;
}

/**
 * The download size (bytes) of a game that is not installed, outside React:
 * the install flow reads it when the row carries no size. 0 when unknown.
 * Shares the hook's cache and in-flight fetch.
 */
export function downloadSizeBytes(appId: number): Promise<number> {
  return fetchSize(appId, cacheKey(appId, false)).catch(() => 0);
}

/**
 * Resolve the install / download size (bytes) for a Steam shortcut
 * appId. Returns `undefined` until the fetch resolves; `0` means the
 * size is unknown (e.g. Ubisoft / Microsoft, or an offline store).
 *
 * @param appId — Steam shortcut app-id, or null to skip.
 * @param installed — current install state. Changing it refetches
 *   (download size → on-disk size) instead of serving a stale value.
 */
export function useGameSize(
  appId: number | null,
  installed: boolean,
): number | undefined {
  const key = appId != null ? cacheKey(appId, installed) : null;
  const [size, setSize] = useState<number | undefined>(
    key != null ? cache.get(key) : undefined,
  );
  // Bumped on invalidation to re-run the effect below. The cache entry is
  // already gone by then, so the re-run is a miss and refetches.
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    if (appId == null) return;
    return onGameSizeInvalidated((invalidated) => {
      if (invalidated === appId) setGeneration((n) => n + 1);
    });
  }, [appId]);

  useEffect(() => {
    if (appId == null || key == null) {
      setSize(undefined);
      return;
    }
    const cached = cache.get(key);
    if (cached != null) {
      setSize(cached);
      return;
    }
    let cancelled = false;
    void fetchSize(appId, key)
      .then((bytes) => {
        if (!cancelled) setSize(bytes);
      })
      .catch(() => {
        /* size is best-effort — leave undefined */
      });
    return () => {
      cancelled = true;
    };
  }, [appId, key, generation]);

  return size;
}
