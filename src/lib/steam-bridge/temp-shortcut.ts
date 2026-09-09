/**
 * Temporary Steam shortcut helpers.
 *
 * Creating a non-Steam shortcut through ``SteamClient.Apps.AddShortcut``
 * is the only reliable way to get an entry Steam will actually launch
 * mid-session : Steam populates its in-memory ``appStore.m_mapApps`` (and
 * the ``gameid`` that ``RunGame`` needs) from this API immediately,
 * whereas a shortcut written straight into ``shortcuts.vdf`` by the
 * backend is only loaded into that map at Steam startup. So a freshly
 * backend-written shortcut launched in the same session fails with
 * Steam's "Game configuration unavailable" modal.
 *
 * Every store auth flow (utils/authShortcutLaunch.ts) and the Ubisoft
 * first-session fallback (utils/ubisoftShortcutLaunch.ts) use these
 * helpers to spin up a throwaway shortcut, launch it, and remove it once
 * its session ends.
 */

import { watchAppStopped } from "./shortcut-types";

const SHORTCUT_POLL_DELAY_MS = 250;
const SHORTCUT_POLL_TIMEOUT_MS = 5000;
/** Safety-net delay for removing a temporary shortcut when the "game
 *  stopped" lifetime notification is never observed (missed event or a
 *  Steam API change). On the happy path the shortcut is removed the
 *  instant the launched "game" stops.
 *
 *  MUST stay strictly larger than the longest launcher ceiling, or it
 *  fires while the window is still up — and removing the shortcut ends
 *  its gamescope session, which destroys the window mid-use. It is
 *  therefore COUPLED to two backend constants; change one, change this:
 *    - `launcher.auth_max_seconds` (600s) — sign-in
 *    - `launcher/flows/storefront._MAX_STOREFRONT_SECONDS` (1800s) plus
 *      its 90s reconcile tail — browsing a shop, which is easily longer
 *      than ten minutes and is why this is no longer 10 * 60 * 1000. */
const TEMP_SHORTCUT_SAFETY_CLEANUP_MS = 35 * 60 * 1000;

/** App store entry. */
interface AppStoreEntry {
  gameid?: unknown;
}

/** App store shape. */
interface AppStoreShape {
  m_mapApps?: {
    get?: (id: number) => AppStoreEntry | undefined;
  };
}

/** App store. */
function appStore(): AppStoreShape | undefined {
  return (window as unknown as { appStore?: AppStoreShape }).appStore;
}

/** Get shortcut game ID string. */
function getShortcutGameIdString(appId: number): string | null {
  const app = appStore()?.m_mapApps?.get?.(appId);
  const gameId = app?.gameid;
  return typeof gameId === "string" && gameId.length > 0 ? gameId : null;
}

/** Poll Steam's in-memory app store until the freshly-created
 *  shortcut has a non-empty ``gameid`` (the signed CRC Steam uses
 *  for ``RunGame``). Resolves ``null`` on timeout. */
async function waitForShortcutGameId(
  appId: number,
  minimumDelayMs = 0,
): Promise<string | null> {
  const startedAt = Date.now();
  const timeoutMs = Math.max(SHORTCUT_POLL_TIMEOUT_MS, minimumDelayMs);
  return new Promise<string | null>((resolve) => {
    const poll = (): void => {
      const elapsed = Date.now() - startedAt;
      if (elapsed >= minimumDelayMs) {
        const gameId = getShortcutGameIdString(appId);
        if (gameId) {
          resolve(gameId);
          return;
        }
      }
      if (elapsed >= timeoutMs) {
        resolve(null);
        return;
      }
      window.setTimeout(poll, SHORTCUT_POLL_DELAY_MS);
    };
    window.setTimeout(poll, SHORTCUT_POLL_DELAY_MS);
  });
}

/** Create a temporary shortcut via Steam's ``AddShortcut`` API.
 *  Returns the new appId once Steam has registered a ``gameid`` for
 *  it, or ``null`` if creation/registration failed (the caller should
 *  surface a "restart Steam" error). The shortcut is removed by
 *  {@link scheduleTemporaryShortcutCleanup} once its session ends. */
export async function createTemporaryShortcut(params: {
  appName: string;
  launcherPath: string;
  launchOptions: string;
  logTag: string;
}): Promise<number | null> {
  const { appName, launcherPath, launchOptions, logTag } = params;
  const steamApps = window.SteamClient?.Apps;
  if (!steamApps?.AddShortcut) return null;
  const startDir = launcherPath.substring(0, launcherPath.lastIndexOf("/"));
  const newAppId = await steamApps.AddShortcut(
    appName,
    launcherPath,
    startDir,
    launchOptions,
  );
  if (typeof newAppId !== "number" || newAppId <= 0) return null;
  steamApps.SetShortcutLaunchOptions?.(newAppId, launchOptions);
  const gameId = await waitForShortcutGameId(newAppId, SHORTCUT_POLL_DELAY_MS);
  if (!gameId) {
    console.error(
      `${logTag} Temp shortcut ${newAppId} never received a gameid`,
    );
    try {
      steamApps.RemoveShortcut?.(newAppId);
    } catch (error) {
      console.error(
        `${logTag} Failed to clean up temp shortcut ${newAppId}:`,
        error,
      );
    }
    return null;
  }
  console.log(
    `${logTag} Temp shortcut ready: appId=${newAppId}, gameId=${gameId}`,
  );
  return newAppId;
}

/** Schedule temporary shortcut cleanup. Removes the entry only once its
 *  launched "game" has actually **stopped** (sign-in completed, aborted,
 *  or the window closed), never on a blind timer.
 *
 *  Why: in Gaming Mode each launched game gets its own gamescope/XWayland
 *  session (``STEAM_MULTIPLE_XWAYLANDS=1``). The login/UPC window renders
 *  on that per-app display. Calling ``RemoveShortcut`` while the game is
 *  still running ends the session and tears down its XWayland, so the
 *  window vanishes back to the Steam UI mid-flow. Waiting for Steam's
 *  "stopped" notification keeps the session — and the window — alive for
 *  the whole login. */
export function scheduleTemporaryShortcutCleanup(
  appId: number,
  logTag: string,
): void {
  const steamApps = window.SteamClient?.Apps;
  const cleanup: Array<() => void> = [];
  let removed = false;

  const remove = (reason: string): void => {
    if (removed) return;
    removed = true;
    for (const fn of cleanup) fn();
    try {
      steamApps?.RemoveShortcut?.(appId);
      console.log(`${logTag} Removed temp shortcut ${appId} (${reason})`);
    } catch (error) {
      console.error(
        `${logTag} Failed to remove temp shortcut ${appId}:`,
        error,
      );
    }
  };

  // The launcher (and with it the window) has exited; the session is gone,
  // so removing the entry now is safe.
  cleanup.push(watchAppStopped(appId, () => remove("game stopped")));

  // Safety net: clean up even if the "stopped" notification never lands,
  // but only well past the launcher's 600s auth ceiling so it can't fire
  // during a normal sign-in.
  const safetyTimer = window.setTimeout(
    () => remove("safety timeout"),
    TEMP_SHORTCUT_SAFETY_CLEANUP_MS,
  );
  cleanup.push(() => window.clearTimeout(safetyTimer));
}
