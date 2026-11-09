/**
 * usePlayStats: the play-bar numbers for one game, from our own sources.
 *
 * Size, total played, last played and cloud-save support, plus the launch
 * circuit breaker. Shared by the native play bar and the legacy
 * `MetaInline`, so both always show the same values.
 *
 *  - Size: `useGameSize` (on-disk once installed, download size before),
 *    falling back to the size on the game record.
 *  - Played: the store's cross-device total (GOG/Epic) when present,
 *    else our own tracked total.
 *  - Last played: our tracking DB, with Steam's own record only as the
 *    fallback when we have none.
 */
import { useEffect, useState } from "react";
import { useCircuitState, type CircuitState } from "./useCircuitState";
import { useGameMetadata } from "./useGameMetadata";
import { useGamePlaytime } from "./useGamePlaytime";
import { useGameSize } from "./useGameSize";
import { useStoreCapability } from "./useStoreCapability";

export interface PlayStatsInput {
  appId: number | null | undefined;
  store?: string | null;
  gameId?: string | null;
  /** Size already known from the game record. */
  sizeBytes?: number;
  installed: boolean;
  /** Read playtime and last played (installed-or-ever-played rows). */
  withPlaytime: boolean;
}

export interface PlayStats {
  /** Bytes, or undefined when unknown. */
  sizeBytes: number | undefined;
  /** Seconds played, when we have a tracked record above zero. */
  playedSecs: number | undefined;
  /** ISO timestamp from our DB, when present. */
  lastPlayedIso: string | null;
  /** Steam's own last-played (unix seconds), only used as a fallback. */
  steamLastPlayed: number | null;
  /** `cloud_saves` tri-state, only for a not-installed game whose store
   *  syncs saves (installed games have the live cloud button instead). */
  cloudSaves: boolean | null | undefined | "hidden";
  circuit: CircuitState;
}

function useSteamLastPlayed(
  appId: number | null | undefined,
  enabled: boolean,
) {
  const [value, setValue] = useState<number | null>(null);
  useEffect(() => {
    if (!enabled || appId == null) return;
    let cancelled = false;
    const apps = (
      window as unknown as {
        SteamClient?: {
          Apps?: {
            GetPlaytime?: (
              id: number,
            ) => Promise<{ rtLastTimePlayed?: number }>;
          };
        };
      }
    ).SteamClient?.Apps;
    if (!apps?.GetPlaytime) return;
    apps
      .GetPlaytime(appId)
      .then((res) => {
        if (!cancelled) setValue(res?.rtLastTimePlayed ?? null);
      })
      .catch(() => {
        /* fallback only */
      });
    return () => {
      cancelled = true;
    };
  }, [appId, enabled]);
  return value;
}

export function usePlayStats(input: PlayStatsInput): PlayStats {
  const { appId, store, gameId, sizeBytes, installed, withPlaytime } = input;
  const metadata = useGameMetadata(appId ?? null);
  const supportsCloudSaves = useStoreCapability(store, "supports_cloud_saves");
  const circuit = useCircuitState(store, gameId);
  const fetchedSize = useGameSize(appId ?? null, installed);
  const playtime = useGamePlaytime(
    withPlaytime ? store : null,
    withPlaytime ? gameId : null,
  );
  const steamLastPlayed = useSteamLastPlayed(appId, withPlaytime);

  const totalSecs =
    playtime != null
      ? playtime.store_total_secs ?? playtime.total_seconds
      : undefined;
  const showCloud = !installed && !!store && supportsCloudSaves;
  return {
    sizeBytes: fetchedSize && fetchedSize > 0 ? fetchedSize : sizeBytes,
    playedSecs: totalSecs != null && totalSecs > 0 ? totalSecs : undefined,
    lastPlayedIso: playtime?.last_played ?? null,
    steamLastPlayed,
    cloudSaves: showCloud ? metadata.data?.cloud_saves : "hidden",
    circuit,
  };
}
