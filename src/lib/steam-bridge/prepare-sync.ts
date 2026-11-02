/**
 * The preparation every sync needs, wherever it is triggered from.
 *
 * There are two callers and they used to disagree. `SyncContext`'s
 * user-triggered syncs (now `syncGames` / `syncImages`) did all three steps
 * below; `AuthDispatcher`, firing the
 * post-login `requestAuthSync`, did none of them. The backend is the same for
 * both (`request_auth_sync` is a one-liner onto `sync_all`), so that gap was
 * the entire difference between a sync the user triggers, which worked, and
 * the automatic one at login, which reportedly "never had any effect".
 *
 * What each step is load-bearing for:
 *
 * - **`uploadActiveSteamUser`**: the backend otherwise resolves the Steam
 *   user from disk heuristics. `set_active_steam_user` in
 *   `rpc/mixins/sync.py` records where that leads: "writing shortcuts.vdf to
 *   the wrong `userdata/<id>` -> 'synced N games, Steam shows 0'". The
 *   frontend runs inside Steam and can read the true active user, so it does.
 * - **`uploadSteamOwnedTitles`**: `appmanifest` only sees *installed* Steam
 *   games, so without a fresh push the Ubisoft Steam-linked filter runs on a
 *   stale snapshot.
 * - **`notifySyncStarted`**: sets `_observedActiveSync` on the store
 *   directly. Without it that flag depends on the `sync_started` event, which
 *   is in `STALE_ON_RELOAD_EVENTS` and is primed past rather than dispatched
 *   on the first poll after a frontend load. Since the restart modal is gated
 *   on `_pendingRestart && _observedActiveSync`, a login sync could write
 *   shortcuts and never tell the user to restart.
 *
 * Extracted rather than copied so the two paths cannot drift apart again.
 */
import { syncStore } from "../../stores/sync-store";
import { uploadActiveSteamUser } from "./active-user";
import { uploadSteamOwnedTitles } from "./owned-library";

/**
 * Bring the backend's view of Steam up to date, then arm the sync store.
 *
 * Await this **before** firing any sync RPC: both uploads exist to be in
 * place before the backend resolves paths and filters, and doing them after
 * the fetch has started is the same as not doing them.
 *
 * Never throws. A failed upload leaves the backend on its previous snapshot,
 * which is strictly what it had before, and is not a reason to abandon the
 * sync the user asked for.
 */
export async function prepareForSync(): Promise<void> {
  syncStore.notifySyncStarted();
  try {
    await uploadActiveSteamUser();
  } catch (e) {
    console.warn("[prepareForSync] uploadActiveSteamUser failed:", e);
  }
  try {
    await uploadSteamOwnedTitles();
  } catch (e) {
    console.warn("[prepareForSync] uploadSteamOwnedTitles failed:", e);
  }
}
