/**
 * Launch a shortcut once with an extra `UNIFIDECK_*` launch option.
 *
 * Steam hands a shortcut's launch options to our launcher as argv, and the
 * launcher promotes `UNIFIDECK_*` tokens into its environment. A token
 * meant for a single launch (stream this installed game from the cloud)
 * is added to the options, the game is started once Steam reports the new
 * options, and the user's own options are put back once Steam has created
 * the game process ({@link restoreStep}).
 *
 * The same order as the wrapper-store launch (`wrapper-shortcut-launch`):
 * `SetShortcutLaunchOptions` applies asynchronously, so `RunGame` waits
 * for it ({@link waitForLaunchOptions}).
 */
import {
  getShortcutDisplayStatus,
  getShortcutRunGameId,
} from "./shortcut-types";
import { waitForLaunchOptions } from "./wrapper-shortcut-launch";

const LOG_TAG = "[OneRunLaunch]";
const RESTORE_POLL_MS = 250;
/** Put the options back by now even if Steam never reports the process,
 *  so the token cannot stick. Long, because Steam's launch can wait on the
 *  user (an interstitial, a controller-config prompt) before it reads the
 *  launch options. */
const RESTORE_TIMEOUT_MS = 60000;
/** Steam display statuses (`EDisplayStatus`, read at runtime). */
const STATUS_LAUNCHING = 1;
const STATUS_RUNNING = 4;

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

/**
 * One poll of the restore: `"restore"` puts the user's options back,
 * `"launching"` notes the launch has begun, `"wait"` polls again.
 *
 * Steam reads a shortcut's launch options only when it creates the
 * process, at the end of its launch steps. It reports Launching (1) as
 * soon as `RunGame` is called, well before that, so restoring on
 * Launching drops the token before Steam ever reads it. Running (4) is set
 * once the process exists, with its argv already built. A launch seen
 * Launching that ends without Running (failed, or cancelled at a prompt)
 * also restores.
 */
export function restoreStep(
  status: number | undefined,
  sawLaunching: boolean,
  timedOut: boolean,
): "restore" | "launching" | "wait" {
  if (status === STATUS_RUNNING || timedOut) return "restore";
  if (status === STATUS_LAUNCHING) return "launching";
  return sawLaunching ? "restore" : "wait";
}

/** Put the user's options back once Steam has created the game process. */
function restoreWhenRunning(appId: number, original: string): void {
  const startedAt = Date.now();
  let sawLaunching = false;
  const tick = (): void => {
    const timedOut = Date.now() - startedAt >= RESTORE_TIMEOUT_MS;
    const step = restoreStep(
      getShortcutDisplayStatus(appId),
      sawLaunching,
      timedOut,
    );
    if (step !== "restore") {
      sawLaunching ||= step === "launching";
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
