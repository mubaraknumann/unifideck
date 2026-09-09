/**
 * Teardown — symmetric cleanup for `definePlugin` unload.
 *
 * Decky's plugin contract requires returning an `unmount`
 * callback from `definePlugin`. This module collects every
 * resource registered at boot time and unregisters them in
 * reverse order. Symmetry with `bootstrap-tasks.ts` :
 * whatever was created there gets removed here.
 *
 * Failures are logged but never thrown — Decky's unmount
 * path is best-effort, an uncaught exception leaves the
 * plugin in a half-loaded state until the next reboot.
 */
import type { RouterPatchHandle } from "./lib/steam-bridge";
import type { CollectionManagerHandle } from "./lib/steam-bridge/collection-manager";
import type { Unregisterable } from "./types/steam";
import { downloadStore } from "./stores/download-store";
import { syncStore } from "./stores/sync-store";
import { authStore } from "./stores/auth-store";
import { storeInfoStore } from "./stores/store-info-store";
/**
 * Handles captured during bootstrap that {@link runTeardown}
 * must dispose on plugin unload. Currently the lifetime
 * listener registration ; will grow as Phase 4 integrates
 * watchdog hooks.
 */
export interface TeardownHandles {
  routerPatch?: RouterPatchHandle | null;
  cacheAutoload?: (() => void) | null;
  libraryPatch?: RouterPatchHandle | null;
  collectionManager?: CollectionManagerHandle | null;
  appStorePatch?: { remove: () => void } | null;
  overviewEnrichment?: (() => void) | null;
  tileStoreBadgePatch?: (() => void) | null;
  appContextMenuPatch?: { unpatch: () => void } | null;
  lifetimeListener?: Unregisterable | null;
  launcherToastPoll?: (() => void) | null;
  pluginUpdateNotice?: (() => void) | null;
  bootEventListener?: (() => void) | null;
}
/**
 * Run every disposer captured during bootstrap, in
 * reverse registration order. Each disposer is
 * isolated so one throwing does not skip the others.
 *
 * Decky Loader calls this on plugin unload — leaks
 * here can survive across reloads of the dev cycle
 * and produce subtle phantom listeners.
 */
/**
 * Every handle's disposer, in teardown order.
 *
 * Typed as a **total** `Record` over `TeardownHandles`, which is the whole
 * point: adding a field to that interface without adding an entry here is
 * a compile error. It used to be a hand-written sequence of `if` blocks,
 * and `overviewEnrichment` was simply missing from it — declared, assigned
 * at boot, never disposed. Nothing caught that, because nothing could.
 *
 * Object literal key order is insertion order, so this table carries the
 * order as well as the coverage: reverse of the registration order in
 * `index.tsx`, so a handle is released before whatever it was built on.
 */
const DISPOSERS: Record<keyof TeardownHandles, (h: TeardownHandles) => void> = {
  bootEventListener: (h) => h.bootEventListener?.(),
  launcherToastPoll: (h) => h.launcherToastPoll?.(),
  pluginUpdateNotice: (h) => h.pluginUpdateNotice?.(),
  tileStoreBadgePatch: (h) => h.tileStoreBadgePatch?.(),
  appContextMenuPatch: (h) => h.appContextMenuPatch?.unpatch(),
  lifetimeListener: (h) => h.lifetimeListener?.unregister(),
  appStorePatch: (h) => h.appStorePatch?.remove(),
  // Releases two window listeners, two EventBusClient subscriptions, the
  // game-size invalidation subscription and the AppMap patch. Skipping this
  // kept an EventBusClient subscriber alive past unload, and the client only
  // stops polling once its subscriber set empties — so every plugin reload
  // left an immortal 2s subscribe_replay loop running against the backend.
  overviewEnrichment: (h) => h.overviewEnrichment?.(),
  collectionManager: (h) => h.collectionManager?.remove(),
  libraryPatch: (h) => h.libraryPatch?.remove(),
  // Same failure mode: its window listener and SHORTCUT_INSTALL_STATE_CHANGED
  // subscription had no disposer at all until this table existed.
  cacheAutoload: (h) => h.cacheAutoload?.(),
  routerPatch: (h) => h.routerPatch?.remove(),
};

export function runTeardown(handles: TeardownHandles): void {
  // Stop boot-time singletons first (they hold EventBus
  // subscriptions and polling timers).
  try {
    syncStore.stop();
  } catch (e) {
    console.warn("[Teardown] syncStore stop failed:", e);
  }
  try {
    downloadStore.stop();
  } catch (e) {
    console.warn("[Teardown] downloadStore stop failed:", e);
  }
  try {
    authStore.stop();
  } catch (e) {
    console.warn("[Teardown] authStore stop failed:", e);
  }
  try {
    storeInfoStore.stop();
  } catch (e) {
    console.warn("[Teardown] storeInfoStore stop failed:", e);
  }
  for (const [name, dispose] of Object.entries(DISPOSERS)) {
    try {
      dispose(handles);
    } catch (e) {
      console.warn(`[Teardown] ${name} disposal failed:`, e);
    }
  }
}
