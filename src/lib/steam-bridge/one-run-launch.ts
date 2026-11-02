/**
 * Launch a shortcut once with an extra `UNIFIDECK_*` launch option.
 *
 * Steam hands a shortcut's launch options to our launcher as argv, and the
 * launcher promotes `UNIFIDECK_*` tokens into its environment. A token
 * meant for a single launch (stream this installed game from the cloud)
 * is added to the options, the game is started once Steam reports the new
 * options, and the user's own options are put back once the game runs.
 *
 * The same order as the wrapper-store launch (`wrapper-shortcut-launch`):
 * `SetShortcutLaunchOptions` applies asynchronously, so `RunGame` waits
 * for it ({@link waitForLaunchOptions}).
 */
import { getShortcutRunGameId, isShortcutAppRunning } from "./shortcut-types";
import { waitForLaunchOptions } from "./wrapper-shortcut-launch";

const LOG_TAG = "[OneRunLaunch]";
const RESTORE_POLL_MS = 250;
/** Put the options back by now even if Steam never reports the app running
 *  (a launch that failed at once), so the token cannot stick. */
const RESTORE_TIMEOUT_MS = 15000;

interface AppDetailsStoreShape {
  GetAppDetails?: (id: number) => { strLaunchOptions?: string } | undefined;
}

function currentLaunchOptions(appId: number): string | null {
  const store = (
    window as unknown as { appDetailsStore?: AppDetailsStoreShape }
  ).appDetailsStore;
  const value = store?.GetAppDetails?.(appId)?.strLaunchOptions;
  return typeof value === "string" ? value : null;
}

/** `options` with `token` appended (any earlier copy of its key removed). */
export function withToken(options: string, token: string): string {
  const key = token.split("=", 1)[0];
  const cleaned = options
    .replace(new RegExp(`(^|\\s)${key}=\\S+`, "g"), " ")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned ? `${cleaned} ${token}` : token;
}

/**
 * Run `appId` once with `token` (e.g. `UNIFIDECK_PLAY_TARGET=cloud`) in its
 * launch options. False when Steam's launch APIs or the shortcut's current
 * options are unavailable; the caller then launches some other way.
 */
export async function launchOnceWithToken(
  appId: number,
  token: string,
): Promise<boolean> {
  const apps = window.SteamClient?.Apps;
  const original = currentLaunchOptions(appId);
  if (!apps?.RunGame || !apps.SetShortcutLaunchOptions || original == null) {
    return false;
  }
  const once = withToken(original, token);
  try {
    apps.SetShortcutLaunchOptions(appId, once);
    await waitForLaunchOptions(appId, once, LOG_TAG);
    apps.RunGame(getShortcutRunGameId(appId), "", -1, 100);
  } catch (error) {
    console.error(`${LOG_TAG} launch failed for appId=${appId}:`, error);
    apps.SetShortcutLaunchOptions?.(appId, original);
    return false;
  }
  restoreWhenRunning(appId, original);
  return true;
}

/** Put the user's options back once Steam has started the app. */
function restoreWhenRunning(appId: number, original: string): void {
  const startedAt = Date.now();
  const tick = (): void => {
    const timedOut = Date.now() - startedAt >= RESTORE_TIMEOUT_MS;
    if (!isShortcutAppRunning(appId) && !timedOut) {
      window.setTimeout(tick, RESTORE_POLL_MS);
      return;
    }
    try {
      window.SteamClient?.Apps?.SetShortcutLaunchOptions?.(appId, original);
    } catch (error) {
      console.error(`${LOG_TAG} restore failed for appId=${appId}:`, error);
    }
  };
  window.setTimeout(tick, RESTORE_POLL_MS);
}
