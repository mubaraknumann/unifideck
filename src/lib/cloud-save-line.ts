/**
 * The cloud-save status line, for every store.
 *
 * Two sources describe a game's saves:
 *
 * - Epic, GOG and EA, which Unifideck syncs itself (`useCloudSaveView`): the
 *   line opens our Download / Upload dialog.
 * - Xbox PC, which the Xodus runtime syncs inside the game
 *   (`useXboxSaveStatus`): the line only reports, and every change waits for
 *   the next launch.
 *
 * Both report the same verdict words (backend `core/save_sync_state.py`), and
 * this module turns either into one line: Steam's own status word when it has
 * one, our text as the fallback, and whether it reads as a problem. The two
 * sources differ only in what a verdict means for the user, which is
 * `SyncModel`.
 */

/** How local saves compare with the cloud (backend `core/save_sync_state.py`). */
export type SaveSyncVerdict =
  | "in_sync"
  | "local_newer"
  | "cloud_newer"
  | "conflict";

/**
 * Who moves the saves. `manual`: Unifideck, before a launch or when the user
 * asks. `runtime`: the game's runtime, at its next launch.
 */
export type SyncModel = "manual" | "runtime";

export interface CloudLine {
  /** Steam's own word for the state, when it has one. */
  token?: string;
  /** Our text, and the fallback for `token`. */
  key: string;
  /** Read as a problem (Steam's sync-failed styling). */
  problem: boolean;
  /** A sync is running. */
  busy: boolean;
}

const line = (
  key: string,
  token?: string,
  extra: Partial<Pick<CloudLine, "problem" | "busy">> = {},
): CloudLine => ({
  key: `play.cloudSave.${key}`,
  token,
  problem: false,
  busy: false,
  ...extra,
});

const IN_SYNC = line("statusInSync", "#AppDetails_CloudStatus_Synchronized");
const OUT_OF_SYNC = "#AppDetails_CloudStatus_OutOfSync";

/** Lines that read the same whatever the store. */
export const CLOUD_LINES = {
  checking: line("statusSyncing", "#AppDetails_CloudStatus_Checking"),
  syncing: line("statusSyncing", "#AppDetails_CloudStatus_Checking", {
    busy: true,
  }),
  checkConnection: line("checkConnection"),
  unresolved: line("statusUnresolved", "#AppDetails_CloudStatus_SyncFailed", {
    problem: true,
  }),
  inSync: IN_SYNC,
} as const;

/**
 * The line for a verdict. A store we sync shows Steam's "out of sync" for any
 * difference, since the user can settle it now. A runtime-synced store says
 * what its next launch will do, and is never a problem: nothing is wrong yet.
 */
export function verdictLine(
  verdict: SaveSyncVerdict,
  model: SyncModel,
): CloudLine {
  if (verdict === "in_sync") return IN_SYNC;
  if (model === "runtime") {
    return verdict === "local_newer"
      ? line("statusWaitingUpload")
      : line("statusWaitingLaunch");
  }
  const key =
    verdict === "local_newer" ? "statusWaitingUpload" : "statusCloudAvailable";
  return line(key, OUT_OF_SYNC, { problem: true });
}
