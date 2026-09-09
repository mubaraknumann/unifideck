/**
 * CollectionManager — auto-generates `[Unifideck] *` Steam Collections
 * mirroring the UNIFIDECK_TABS list.
 *
 * Ported from `staging:src/spoofing/CollectionManager.ts`. Wires into
 * Steam's `window.collectionStore` and `window.appStore` — the only
 * two Steam globals not abstracted by `SteamBridge` because the
 * collection APIs are too coupled to Steam's React tree to project.
 */
import i18n from "i18next";
import {
  getUnifideckTabs,
  isTabMasterInstalled,
  type UnifideckTab,
} from "./tab-container";
import { COMPAT_TAB_TITLE_KEYS, awaitDeviceType } from "../device-type";
import { runFilters } from "../library-filters";
import { EventBusClient } from "../../api/event-bus-client";
import { Events } from "../../types/events";
import type { SteamAppOverview } from "../../types/steam";

const COLLECTION_PREFIX = "[Unifideck] ";
// Non-Steam shortcuts (ours + the user's) carry this app_type; exclude
// them so we never feed Ubisoft titles back into the Steam-owned filter.
const NON_STEAM_SHORTCUT_APP_TYPE = 1073741824;

/**
 * Master opt-in for `[Unifideck]` Steam Collections. Default OFF — except
 * when TabMaster is installed, where it defaults ON (TabMaster suppresses
 * our own injected library tabs, so collections are the only organization
 * we can offer those users).
 *
 * Steam keeps ALL user collections in one CEF-localStorage blob and
 * auto-syncs that whole namespace to Steam Cloud, so any collection we
 * create propagates to the user's other devices — even ones without the
 * plugin — and into Desktop mode. There is no per-collection "local only"
 * flag. So collections are opt-in: most users get the in-React-tree
 * library tabs (which sync nowhere) and never touch the cloud.
 *
 * Stored as "1"/"0" so an explicitly-chosen value is distinguishable
 * from "never set" (the grandfather migration below relies on that).
 * `setCollectionsEnabled` broadcasts `COLLECTIONS_ENABLED_EVENT` so the
 * QAM toggle and the running manager stay in sync without a restart.
 */
export const COLLECTIONS_ENABLED_KEY = "unifideck:collections.enabled";
export const COLLECTIONS_ENABLED_EVENT = "unifideck:collections-enabled-change";

/**
 * Grandfather marker. Before collections became opt-in they were created
 * for everyone, so on the first run of this build we keep them for users
 * who already have them (see `migrateGrandfatherExisting`). Set once so
 * the detection never re-runs.
 */
const COLLECTIONS_MIGRATED_KEY = "unifideck:collections.migrated";

/**
 * One-time-cleanup marker for the OFF state. When collections are off we
 * delete any leftover `[Unifideck]` collections exactly ONCE, then leave
 * the blob alone. Cleaning on every boot would thrash against a *different*
 * device that has them enabled and keeps re-creating them — collections
 * are account-global, so an OFF device can't truly hide what an ON device
 * insists on creating.
 */
const COLLECTIONS_CLEANED_KEY = "unifideck:collections.cleaned";

/** Coalescing window for install/uninstall-driven rebuilds. Long enough that
 *  a queued batch of installs triggers one rebuild, short enough that a single
 *  install feels immediate. */
const COLLECTION_RESYNC_DEBOUNCE_MS = 1500;

export function isCollectionsEnabled(): boolean {
  try {
    const v = window.localStorage.getItem(COLLECTIONS_ENABLED_KEY);
    if (v === "1") return true;
    if (v === "0") return false;
  } catch {
    /* fall through to the default */
  }
  // No explicit choice yet — default ON when TabMaster is installed (our
  // own tabs are suppressed under it), OFF for everyone else. TabMaster
  // detection is synchronous (reads DeckyPluginLoader), so this is safe to
  // call before the collection store hydrates.
  return isTabMasterInstalled();
}

/** True once the user (or the grandfather migration) has chosen a value. */
function isEnabledExplicit(): boolean {
  try {
    return window.localStorage.getItem(COLLECTIONS_ENABLED_KEY) != null;
  } catch {
    return false;
  }
}

export function setCollectionsEnabled(on: boolean): void {
  try {
    window.localStorage.setItem(COLLECTIONS_ENABLED_KEY, on ? "1" : "0");
  } catch {
    /* localStorage unavailable — worst case is pre-fix behavior */
  }
  try {
    window.dispatchEvent(
      new CustomEvent(COLLECTIONS_ENABLED_EVENT, { detail: on }),
    );
  } catch {
    /* CEF can deny CustomEvent in rare half-loaded states */
  }
}

function isCleaned(): boolean {
  try {
    return window.localStorage.getItem(COLLECTIONS_CLEANED_KEY) === "1";
  } catch {
    return false;
  }
}

function setCleaned(on: boolean): void {
  try {
    if (on) window.localStorage.setItem(COLLECTIONS_CLEANED_KEY, "1");
    else window.localStorage.removeItem(COLLECTIONS_CLEANED_KEY);
  } catch {
    /* ignore */
  }
}

function hasMigrated(): boolean {
  try {
    return window.localStorage.getItem(COLLECTIONS_MIGRATED_KEY) === "1";
  } catch {
    return false;
  }
}

function setMigrated(): void {
  try {
    window.localStorage.setItem(COLLECTIONS_MIGRATED_KEY, "1");
  } catch {
    /* ignore */
  }
}

/**
 * One-time grandfather migration. Collections used to be created for
 * everyone, so users already relying on them should keep them. On the
 * first run of the opt-in build, if the user has never chosen the setting
 * AND `[Unifideck]` collections already exist in the store, default this
 * account to ENABLED so we don't pull away tabs they use. Fresh installs
 * (no such collections) stay OFF. Writes localStorage directly — NOT via
 * `setCollectionsEnabled` — so it doesn't fire the toggle event (this is a
 * silent default, not a user action). Caller must ensure the store is
 * hydrated first so the collection scan is meaningful.
 */
function migrateGrandfatherExisting(cs: CollectionStore): void {
  if (hasMigrated()) return;
  if (!isEnabledExplicit() && snapshotUnifideckCollections(cs).length > 0) {
    try {
      window.localStorage.setItem(COLLECTIONS_ENABLED_KEY, "1");
    } catch {
      /* ignore — falls back to OFF, recoverable via the QAM toggle */
    }
  }
  setMigrated();
}

interface AppStoreOverview {
  appid: number;
  display_name: string;
  app_type?: number;
  installed?: boolean;
  steam_deck_compat_category?: number;
}

interface Collection {
  AsDragDropCollection: () => {
    AddApps: (overviews: AppStoreOverview[]) => void;
    RemoveApps: (overviews: AppStoreOverview[]) => void;
  };
  Save: () => Promise<void>;
  Delete: () => Promise<void>;
  allApps: AppStoreOverview[];
  displayName: string;
  id: string;
}

interface CollectionStore {
  GetCollection: (id: string) => Collection | undefined;
  GetCollectionIDByUserTag: (tag: string) => string | null;
  NewUnsavedCollection: (
    tag: string,
    filter: unknown,
    overviews: AppStoreOverview[],
  ) => Collection | undefined;
  userCollections: Map<string, Collection>;
}

interface AppStore {
  GetAppOverviewByAppID: (appId: number) => AppStoreOverview | null;
}

function getCollectionStore(): CollectionStore | null {
  return (
    (window as unknown as { collectionStore?: CollectionStore })
      .collectionStore ?? null
  );
}

function getAppStore(): AppStore | null {
  return (window as unknown as { appStore?: AppStore }).appStore ?? null;
}

function tabName(tab: UnifideckTab): string {
  return `${COLLECTION_PREFIX}${tab.title}`;
}

/**
 * Names `cleanupStaleCollections` must NOT delete.
 *
 * The compat tab's title is named after the device ("Great on Deck" vs
 * "Great on Machine"), but collections are account-global and synced
 * through Steam Cloud. So a user with both a Deck and a Steam Machine
 * would have each device treat the other's compat collection as stale
 * and delete it, on every boot, forever. Recognise all three device
 * names as valid even though only the local one is ever created.
 */
function validCollectionNames(): Set<string> {
  const names = new Set(getUnifideckTabs().map(tabName));
  for (const key of COMPAT_TAB_TITLE_KEYS) {
    names.add(`${COLLECTION_PREFIX}${i18n.t(key)}`);
  }
  return names;
}

async function deleteCollection(c: Collection): Promise<void> {
  try {
    await c.Delete();
  } catch (e) {
    console.error(`[Unifideck Collections] delete ${c.displayName} failed`, e);
  }
}

/**
 * Snapshot of every `[Unifideck]` collection. `Delete()` mutates the
 * underlying MobX Map, so iterating `userCollections.values()` live
 * while deleting skips entries — always work from a snapshot.
 */
function snapshotUnifideckCollections(cs: CollectionStore): Collection[] {
  let collections: Map<string, Collection> | undefined;
  try {
    collections = cs.userCollections;
  } catch {
    return [];
  }
  if (!collections || typeof collections.values !== "function") return [];
  return Array.from(collections.values()).filter((c) =>
    c?.displayName?.startsWith(COLLECTION_PREFIX),
  );
}

async function cleanupStaleCollections(): Promise<void> {
  const cs = getCollectionStore();
  if (!cs) return;
  const valid = validCollectionNames();
  for (const c of snapshotUnifideckCollections(cs)) {
    if (!valid.has(c.displayName)) {
      await deleteCollection(c);
    }
  }
}

async function getOrCreateCollection(tag: string): Promise<Collection | null> {
  const cs = getCollectionStore();
  if (!cs) return null;
  const id = cs.GetCollectionIDByUserTag(tag);
  if (typeof id === "string") {
    const existing = cs.GetCollection(id);
    if (existing) return existing;
  }
  const created = cs.NewUnsavedCollection(tag, undefined, []);
  if (!created) return null;
  await created.Save();
  return created;
}

async function clearCollection(c: Collection): Promise<void> {
  const apps = c.allApps ?? [];
  if (apps.length === 0) return;
  c.AsDragDropCollection().RemoveApps(apps);
  await c.Save();
}

async function syncTab(
  tab: UnifideckTab,
  allApps: SteamAppOverview[],
): Promise<boolean> {
  const matches = allApps.filter(
    (a) => a.appid > 0 && runFilters(tab.filters, a),
  );
  if (matches.length === 0) {
    // Nothing to show — don't create an empty `[Unifideck]` shell, and
    // drop any leftover one from a previous sync.
    const cs = getCollectionStore();
    if (!cs) return false;
    const id = cs.GetCollectionIDByUserTag(tabName(tab));
    if (typeof id === "string") {
      const existing = cs.GetCollection(id);
      if (existing) await deleteCollection(existing);
    }
    return true;
  }
  const c = await getOrCreateCollection(tabName(tab));
  if (!c) return false;
  const appStore = getAppStore();
  if (!appStore) return false;
  await clearCollection(c);
  const overviews: AppStoreOverview[] = [];
  for (const a of matches) {
    try {
      const o = appStore.GetAppOverviewByAppID(a.appid);
      if (o) overviews.push(o);
    } catch {
      /* skip */
    }
  }
  if (overviews.length > 0) {
    c.AsDragDropCollection().AddApps(overviews);
    await c.Save();
  }
  return true;
}

/** Sync every `[Unifideck]` collection to current tab filters. */
export async function syncUnifideckCollections(): Promise<void> {
  if (!isCollectionsEnabled()) return;
  if (!isCollectionsAvailable()) return;
  // Collection names are device-specific ("Great on Deck" vs "Great on
  // Machine") AND account-global + cloud-synced. This runs at plugin
  // init, before the device-type RPC has answered, so without this
  // await a Steam Machine would create "[Unifideck] Great on Deck" from
  // the cached default and push it to every device on the account —
  // where it is indistinguishable from a real sibling device's
  // collection, and so deliberately never cleaned up.
  //
  // Transient UI is allowed to be briefly wrong and self-correct.
  // Persistent cloud state is not.
  await awaitDeviceType();
  await cleanupStaleCollections();
  const cs = getCollectionStore();
  if (!cs) return;
  let allApps: SteamAppOverview[] = [];
  try {
    const games = cs.GetCollection("type-games");
    allApps = (games?.allApps ?? []) as unknown as SteamAppOverview[];
  } catch {
    return;
  }
  if (allApps.length === 0) return;
  await Promise.allSettled(getUnifideckTabs().map((t) => syncTab(t, allApps)));
}

/**
 * Display names of every Steam game the user owns — installed or not —
 * from Steam's "type-games" collection. Non-Steam shortcuts are excluded
 * (see {@link NON_STEAM_SHORTCUT_APP_TYPE}). `appmanifest` only knows
 * installed games, so this is the only way the backend learns about
 * owned-but-not-installed Steam games.
 */
export function collectSteamOwnedGameTitles(): string[] {
  const cs = getCollectionStore();
  if (!cs) return [];
  let allApps: SteamAppOverview[] = [];
  try {
    const games = cs.GetCollection("type-games");
    allApps = (games?.allApps ?? []) as unknown as SteamAppOverview[];
  } catch {
    return [];
  }
  const titles = new Set<string>();
  for (const a of allApps) {
    if (!a || a.appid <= 0 || a.app_type === NON_STEAM_SHORTCUT_APP_TYPE) {
      continue;
    }
    const name = a.display_name?.trim();
    if (name) titles.add(name);
  }
  return Array.from(titles);
}

/**
 * Delete every `[Unifideck]` collection. Verifies the store actually
 * dropped them (deletes persist asynchronously and can race a Steam
 * restart), retrying leftovers a few times before giving up. Re-creation
 * is governed by the enabled flag / cleaned marker, not by this function.
 */
export async function deleteAllUnifideckCollections(): Promise<void> {
  const cs = getCollectionStore();
  if (!cs) return;
  // Tag-based pass first — deterministic lookup for the current locale;
  // the prefix scan below also catches collections created under a
  // different UI language.
  for (const tab of getUnifideckTabs()) {
    try {
      const id = cs.GetCollectionIDByUserTag(tabName(tab));
      if (typeof id === "string") {
        const c = cs.GetCollection(id);
        if (c) await deleteCollection(c);
      }
    } catch {
      /* skip */
    }
  }
  for (let attempt = 0; attempt < 3; attempt++) {
    const targets = snapshotUnifideckCollections(cs);
    if (targets.length === 0) return;
    for (const c of targets) await deleteCollection(c);
    await new Promise((r) => setTimeout(r, 250));
  }
  const survivors = snapshotUnifideckCollections(cs);
  if (survivors.length > 0) {
    console.error(
      "[Unifideck Collections] collections survived deletion:",
      survivors.map((c) => c.displayName),
    );
  }
}

export function isCollectionsAvailable(): boolean {
  const s = getCollectionStore();
  if (
    !s ||
    typeof s.GetCollectionIDByUserTag !== "function" ||
    typeof s.NewUnsavedCollection !== "function"
  )
    return false;
  try {
    // ``GetCollection("type-games")`` is a synchronous lookup
    // by tag; it accesses the underlying store directly without
    // going through the ``userCollections`` MobX-computed getter
    // that throws when the store is half-hydrated. Once
    // ``type-games`` is resolvable with a real ``allApps`` array,
    // the whole collection graph is safe to traverse.
    const games = s.GetCollection("type-games");
    return Boolean(games && Array.isArray(games.allApps));
  } catch {
    return false;
  }
}

/** Manager handle returned by `startCollectionManager`. */
export interface CollectionManagerHandle {
  resync(): Promise<void>;
  remove(): void;
}

async function waitForCollections(timeoutMs = 30_000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (isCollectionsAvailable()) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

/**
 * Collection manager. Collections are opt-in ({@link isCollectionsEnabled}):
 *
 * - ON  → subscribe to `unifideck-sync-completed` and rebuild the
 *   `[Unifideck]` collections after every library sync (original behavior).
 * - OFF → delete any leftover `[Unifideck]` collections exactly once (see
 *   {@link isCleaned}) and otherwise stay out of the way.
 *
 * On first run it grandfathers users who already have collections to ON
 * (see {@link migrateGrandfatherExisting}). Reacts live to
 * `COLLECTIONS_ENABLED_EVENT` (the QAM toggle) so flipping the setting
 * takes effect without a Steam restart — turning it OFF deletes the
 * collections immediately. `remove()` detaches every listener.
 */
export function startCollectionManager(): CollectionManagerHandle {
  let syncAttached = false;
  const onSync = () => {
    void syncUnifideckCollections().catch((e) =>
      console.error("[Unifideck Collections] resync failed", e),
    );
  };

  // Install/uninstall used to reach the collections only via the NEXT library
  // sync (or a Steam restart), so "[Unifideck] Installed" — and any TabMaster
  // tab built on it — lagged behind reality.
  //
  // This listened on GAME_INSTALLED, which has NO backend emitter: its only
  // emit site sat in `core/manifest.py`'s `discover_all`, which nothing calls.
  // So installs never reached the collections while uninstalls did (via
  // GAME_UNINSTALLED, which every store's uninstall path really emits) — the
  // exact lag this block claimed to fix. SHORTCUT_INSTALL_STATE_CHANGED is the
  // live event: `ShortcutService.mark_installed` / `mark_uninstalled` emit it in
  // both directions, and `cleanup_finalize` emits it per cleared game.
  // GAME_UNINSTALLED is kept as well — redundant under the debounce, but it
  // covers the edge where `mark_uninstalled` finds no shortcut and returns
  // without emitting.
  //
  // Debounced because a multi-game operation emits a burst and each rebuild
  // walks every collection. The delay is also load-bearing for correctness:
  // `runFilters` reads the install status that `lib/library-filters` keeps in
  // memory, and library-filters flips it from ITS OWN subscription to this same
  // event. Handlers for one poll run synchronously, so deferring the rebuild
  // guarantees it observes the flip. Lowering this toward 0 would make the
  // rebuild race that update and silently omit the just-installed game.
  let installDebounce: number | undefined;
  const onInstallChange = () => {
    window.clearTimeout(installDebounce);
    installDebounce = window.setTimeout(onSync, COLLECTION_RESYNC_DEBOUNCE_MS);
  };
  let unsubInstallEvents: Array<() => void> = [];

  const attachSync = () => {
    if (syncAttached) return;
    window.addEventListener("unifideck-sync-completed", onSync);
    unsubInstallEvents = [
      EventBusClient.subscribe(
        Events.SHORTCUT_INSTALL_STATE_CHANGED,
        onInstallChange,
      ),
      EventBusClient.subscribe(Events.GAME_UNINSTALLED, onInstallChange),
    ];
    syncAttached = true;
  };
  const detachSync = () => {
    if (!syncAttached) return;
    window.removeEventListener("unifideck-sync-completed", onSync);
    window.clearTimeout(installDebounce);
    for (const off of unsubInstallEvents) off();
    unsubInstallEvents = [];
    syncAttached = false;
  };

  // Build/maintain collections for the ON state. `syncUnifideckCollections`
  // self-guards on the enabled flag and store readiness, so this is safe to
  // call eagerly.
  const enabledSync = () =>
    void syncUnifideckCollections().catch((e) =>
      console.error("[Unifideck Collections] sync failed", e),
    );

  // Drop leftover collections for the OFF state, once.
  const cleanupOnce = () => {
    if (isCleaned()) return;
    void deleteAllUnifideckCollections()
      .then(() => setCleaned(true))
      .catch((e) => console.error("[Unifideck Collections] cleanup failed", e));
  };

  // React to the QAM toggle without a restart.
  const onEnabledChange = (e: Event) => {
    const enabled = Boolean((e as CustomEvent<boolean>).detail);
    if (enabled) {
      // Opted back in — let a future opt-out clean up again, then rebuild.
      setCleaned(false);
      attachSync();
      enabledSync();
    } else {
      // Opted out — collections are deleted immediately.
      detachSync();
      void deleteAllUnifideckCollections()
        .then(() => setCleaned(true))
        .catch((err) =>
          console.error("[Unifideck Collections] disable cleanup failed", err),
        );
    }
  };
  window.addEventListener(COLLECTIONS_ENABLED_EVENT, onEnabledChange);

  // Boot: wait for the store to hydrate, grandfather existing users, then
  // apply the resolved ON/OFF state. We can only decide after migration
  // (which needs the store), so the sync listener is attached here rather
  // than synchronously — the initial sync below covers anything missed.
  void waitForCollections()
    .then((ready) => {
      const cs = ready ? getCollectionStore() : null;
      if (cs) migrateGrandfatherExisting(cs);
      if (isCollectionsEnabled()) {
        attachSync();
        if (ready) enabledSync();
        else
          console.warn(
            "[Unifideck Collections] store never became ready — skipping initial sync",
          );
      } else if (ready) {
        cleanupOnce();
      }
    })
    .catch((e) => console.error("[Unifideck Collections] startup failed", e));

  return {
    resync: syncUnifideckCollections,
    remove: () => {
      detachSync();
      window.removeEventListener(COLLECTIONS_ENABLED_EVENT, onEnabledChange);
    },
  };
}
