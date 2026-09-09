/**
 * useSyncCooldown — manual-sync rate limiter.
 *
 * After a sync completes, the manual Sync button is disabled
 * for `cooldownMs` so users don't hammer the backend with
 * redundant runs. The cooldown is module-level so it survives
 * QAM dismounts (legacy behaviour from staging's
 * `LibrarySync.tsx` cooldown timer).
 *
 * Listens for `SYNC_COMPLETE` / `SYNC_FAILED` /
 * `SYNC_CANCELLED` to start the cooldown ; a 1-second
 * interval re-renders consumers so the countdown is visible.
 *
 * The cooldown duration is configurable at runtime via
 * `setSyncCooldownMs`, which `SyncStore._pollOnce` calls with the
 * `cooldown_ms` field of `get_sync_progress()` — so power users can
 * tune it via the backend config without a code change. (That field
 * comes from `SyncService.get_status()`; the `get_sync_status` route
 * that once fronted it was a dead alias of `get_sync_progress` and
 * was deleted in the audit §1.2 pass.) Default matches staging (5s)
 * to undo the 30s regression that shipped in for-pr-0.7.
 */
import { useEffect, useState } from "react";
import { useEventBus } from "../api/event-bus-client";
import { Events } from "../types/events";

const DEFAULT_COOLDOWN_MS = 5_000;

/** Module-level so the cooldown survives QAM dismount/remount.
 *  Stores the unix-ms timestamp when the cooldown expires. */
let cooldownEndsAt = 0;
/** Active cooldown duration. Mutable via `setSyncCooldownMs`. */
let cooldownMs = DEFAULT_COOLDOWN_MS;

/** Override the active cooldown. Called by `SyncStore._pollOnce`
 *  with the `cooldown_ms` field of `get_sync_progress()`.
 *  Falls back to the default if the value is invalid. */
export function setSyncCooldownMs(ms: number): void {
  if (!Number.isFinite(ms) || ms < 0) return;
  cooldownMs = ms;
}

/** Cooldown state — `canSync` flips back to true once the
 *  cooldown expires ; `remainingSecs` is updated every 1s
 *  for a live countdown display. */
export interface UseSyncCooldownResult {
  canSync: boolean;
  remainingSecs: number;
}

/** Read live cooldown state. */
export function useSyncCooldown(): UseSyncCooldownResult {
  const [now, setNow] = useState<number>(Date.now);

  // Tick once a second so the countdown re-renders. Stops
  // ticking when the cooldown is over (saves a render/sec).
  useEffect(() => {
    if (cooldownEndsAt <= now) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [now]);

  // On any terminal sync event, arm the cooldown and trigger
  // a re-render so the timer immediately reflects the change.
  useEventBus(Events.SYNC_COMPLETE, () => {
    cooldownEndsAt = Date.now() + cooldownMs;
    setNow(Date.now());
  });
  useEventBus(Events.SYNC_FAILED, () => {
    cooldownEndsAt = Date.now() + cooldownMs;
    setNow(Date.now());
  });
  useEventBus(Events.SYNC_CANCELLED, () => {
    cooldownEndsAt = Date.now() + cooldownMs;
    setNow(Date.now());
  });

  const remainingMs = Math.max(0, cooldownEndsAt - now);
  return {
    canSync: remainingMs === 0,
    remainingSecs: Math.ceil(remainingMs / 1000),
  };
}
