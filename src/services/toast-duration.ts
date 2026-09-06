/**
 * toast-duration — how long a backend-driven toast stays up.
 *
 * Shared by the two LAUNCHER_STAGE renderers, which reach the same
 * payloads by different routes: `boot-event-listener.tsx` off the plugin
 * bus's replay buffer, `launcherToasts.tsx` off the launcher subprocess's
 * bridge file. A toast must not read differently depending on which
 * process emitted it, so the rule lives here once.
 *
 * Both renderers previously hardcoded the severity defaults and ignored
 * `duration_ms` entirely, even though the backend has always been able to
 * send it and `ToastActionPayload` has always declared it. The message
 * that exposed this is the shortcut write-refusal paragraph, which asks
 * for 12s and was being cut at 7.5s.
 */

/** Toast duration when the payload says nothing (informational). */
export const DEFAULT_DURATION = 5000;
/** Toast duration when the payload says nothing and severity is loud. */
export const ERROR_DURATION = 7500;

/** Floor — below this a toast is gone before it can be read. */
const MIN_DURATION = 1500;
/** Ceiling — above this a toast stops being a toast. */
const MAX_DURATION = 30000;

/**
 * Resolve a toast's on-screen duration in milliseconds.
 *
 * An explicit `duration_ms` from the backend wins, clamped so a bad
 * value can neither flash the toast past the user nor pin it to the
 * screen. Anything non-finite or non-positive is treated as absent
 * rather than clamped, so a `0` or a `NaN` from a malformed payload
 * falls back to the severity default instead of becoming 1.5s.
 */
export function resolveToastDuration(
  durationMs: number | undefined,
  severity?: "info" | "warning" | "error",
): number {
  if (
    typeof durationMs === "number" &&
    Number.isFinite(durationMs) &&
    durationMs > 0
  ) {
    return Math.min(Math.max(durationMs, MIN_DURATION), MAX_DURATION);
  }
  const longer = severity === "error" || severity === "warning";
  return longer ? ERROR_DURATION : DEFAULT_DURATION;
}
