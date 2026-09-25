/**
 * Library tab filters — pure predicates over `SteamAppOverview`.
 *
 * Powers the custom Unifideck library tabs. Each filter receives an
 * app overview and returns whether the app belongs in the tab. The
 * Unifideck game cache (store-of-record for non-Steam shortcuts we
 * manage) and the third-party shortcut allow-list are populated by
 * `LibraryContext` from `get_all_unifideck_games`.
 *
 * Ported from `staging:src/tabs/filters.ts`. The module-level caches
 * mirror staging's design — keeping them outside React lets the
 * Steam library-patch hook call `runFilters` from a non-component
 * context without prop-drilling.
 */
import { call } from "@decky/api";
import { unwrapRpcEnvelope } from "../../api/useRPC";
import { EventBusClient } from "../../api/event-bus-client";
import { Events } from "../../types/events";
import {
  getCachedCompatByTitle,
  getCachedRating,
  loadCompatCacheFromBackend,
  meetsGreatOnCurrentDevice,
} from "../protondb-cache";
import { getCompatByShortcutAppId, loadFacets } from "../library-facets";
import { activeCompatTrack } from "../device-type";
import {
  isTopRated,
  overviewCompatCategory,
} from "../steam-bridge/compat-packed";
import { invalidateGameSize } from "../game-size-cache";
import { STORE_PRIORITY } from "../game-grouping";
import { isGroupDuplicatesEnabled } from "../group-duplicates-setting";
import type { SteamAppOverview } from "../../types/steam";

export type StoreSlug =
  | "steam"
  | "epic"
  | "gog"
  | "amazon"
  | "ubisoft"
  | "battlenet"
  | "microsoft"
  | "gamevault"
  | "itch";

export type FilterType =
  | "installed"
  | "platform"
  | "store"
  | "deckCompat"
  | "all"
  | "nonSteam";

export interface FilterParams {
  installed: { installed: boolean };
  platform: { platform: "steam" | "nonSteam" | "all" };
  store: { store: StoreSlug | "all" };
  deckCompat: Record<string, never>;
  all: Record<string, never>;
  nonSteam: Record<string, never>;
}

export interface TabFilter<T extends FilterType = FilterType> {
  type: T;
  params: FilterParams[T];
}

const NON_STEAM_APP_TYPE = 1073741824;

interface UnifideckCacheEntry {
  store: Exclude<StoreSlug, "steam">;
  isInstalled: boolean;
  steamAppId?: number;
  storeGameId?: string;
  title?: string;
  dedupeGroupId?: string;
  editionLabel?: string;
  /** Real Steam AppID of a native copy of this title the user already
   *  owns — NOT the same as `steamAppId` above (that's a metadata-
   *  spoofing match to the Steam *store catalog*, unrelated to whether
   *  the user owns it). Set by `core.game_grouping`'s Steam
   *  cross-reference. */
  steamOwnedAppId?: number;
  /** Edition/variant suffix extracted from the Steam-owned copy's OWN
   *  title (e.g. "The Final Cut"), independent of this entry's own
   *  `editionLabel` — title-matching tolerates edition differences, so
   *  the two are not guaranteed to agree. */
  steamOwnedEditionLabel?: string;
}

/** Stored in both signed and unsigned forms — Steam returns the
 *  appid signed in some surfaces and unsigned in others, and the
 *  filter functions can't predict which. */
export const unifideckGameCache: Map<number, UnifideckCacheEntry> = new Map();

/** One cross-store sibling of a duplicate-title group, as surfaced to
 *  the detail-page store switcher. `store: "steam"` only ever appears
 *  as the synthetic entry {@link getGroupSiblings} adds for a real
 *  Steam-owned copy — Unifideck itself never manages a "steam" store. */
export interface GroupSibling {
  appId: number;
  store: StoreSlug;
  title: string;
  editionLabel?: string;
}

/** Reverse index: ``dedupe_group_id`` → every sibling appId in that
 *  group. Populated alongside ``unifideckGameCache``. Only groups with
 *  more than one member are stored — a lone game has nothing to switch
 *  to, so it isn't worth a Map entry. */
const dedupeGroupSiblings: Map<string, GroupSibling[]> = new Map();

/** Reverse index: real Steam AppID (``steamOwnedAppId``) → a bridging
 *  Unifideck shortcut appId that matched it. Lets {@link getGroupSiblings}
 *  answer when queried with the *real* Steam appid — e.g. the user opened
 *  the native Steam app-details page for a game they also have on Epic —
 *  which never appears as a key in ``unifideckGameCache`` on its own
 *  (that cache is keyed by Unifideck shortcut appIds only). Real Steam
 *  catalog appids are small positive ints, so no signed/unsigned variant
 *  handling is needed here (unlike shortcut appIds). */
const steamOwnedReverseAppId: Map<number, number> = new Map();

/** Every other store's copy of the game at ``appId`` — title-matched by
 *  the backend, cross-store siblings plus (when present) a synthetic
 *  ``"steam"`` entry for a real Steam-owned copy — or ``[]`` when this
 *  game has neither and the cache hasn't loaded yet. The detail-page
 *  store switcher and the "All Games" grid's badge cluster both read
 *  from this single source so they agree on what "the same game" means.
 *  Unlike cross-store siblings (only meaningful in groups of 2+), the
 *  Steam entry is added even for an otherwise-ungrouped (singleton)
 *  title, since "also owned on Steam" doesn't require a second
 *  Unifideck-managed copy to be worth surfacing. */
export function getGroupSiblings(appId: number): GroupSibling[] {
  const entry = unifideckGameCache.get(appId);
  if (!entry) {
    // Not a Unifideck shortcut appId — might be the real Steam appid of a
    // game we also have on another store. Bridge to the Unifideck side and
    // let that lookup build the full sibling list (it'll include a "steam"
    // entry for this exact appId).
    const bridgeAppId = steamOwnedReverseAppId.get(appId);
    return bridgeAppId != null ? getGroupSiblings(bridgeAppId) : [];
  }

  const crossStoreSiblings = entry.dedupeGroupId
    ? dedupeGroupSiblings.get(entry.dedupeGroupId) ?? []
    : [];

  // The Steam cross-reference is matched per-game (title_match isn't
  // guaranteed perfectly transitive), so a sibling other than the
  // queried appId may be the one carrying steamOwnedAppId. Any member
  // finding it means the whole group counts as Steam-owned — and its
  // steamOwnedEditionLabel travels with it, from that SAME entry (not
  // any other member's), since it describes the Steam title specifically.
  const steamOwnedSource: UnifideckCacheEntry | undefined =
    entry.steamOwnedAppId != null
      ? entry
      : crossStoreSiblings
          .map((s) => unifideckGameCache.get(s.appId))
          .find((e): e is UnifideckCacheEntry => e?.steamOwnedAppId != null);

  const steamOwnedAppId = steamOwnedSource?.steamOwnedAppId;
  if (!steamOwnedAppId) return crossStoreSiblings;

  const steamEntry: GroupSibling = {
    appId: steamOwnedAppId,
    store: "steam",
    title: entry.title ?? "",
    editionLabel: steamOwnedSource?.steamOwnedEditionLabel,
  };
  // No cross-store group of its own, but a real Steam copy exists —
  // still worth a 2-entry switcher (this Unifideck copy + Steam).
  if (crossStoreSiblings.length === 0) {
    return [
      {
        appId,
        store: entry.store,
        title: entry.title ?? "",
        editionLabel: entry.editionLabel,
      },
      steamEntry,
    ];
  }
  return [...crossStoreSiblings, steamEntry];
}

/** Every appId that's redundant once "Group duplicates" is on — a
 *  better tile already represents the same title, so this one is hidden
 *  from "All Games", "Great on Deck", and "Installed" alike (via
 *  {@link hideAsDuplicate}). Two reasons feed this set:
 *
 *  1. Not the chosen "primary" of a cross-store duplicate group — the
 *     surviving tile is whichever store's shortcut
 *     {@link pickGroupPrimary} chose.
 *  2. The game is also owned on real native Steam
 *     (`steamOwnedAppId` set) — the native Steam tile already shows
 *     unconditionally, so every Unifideck copy is redundant, primary or
 *     not.
 *
 *  Populated alongside ``unifideckGameCache`` — unconditionally,
 *  regardless of the "Group duplicates" setting, since membership here
 *  answers "is this a duplicate", not "should it be hidden right now".
 *  Steam's native tile renderer (unlike our own `GameGrid`) can't show a
 *  multi-store badge cluster, so grouping is a plain hide rather than a
 *  merge — the detail-page store switcher (`GameStoreSwitcher`) is how
 *  the other stores (Steam included) stay reachable regardless of the
 *  setting. */
const nonPrimaryDuplicateAppIds: Set<number> = new Set();

/** True if ``appId`` is a redundant copy of an already-shown title — see
 *  {@link nonPrimaryDuplicateAppIds} for the two reasons. Does NOT
 *  account for the "Group duplicates" setting — see {@link
 *  hideAsDuplicate} for the gated version the tab filters actually use.
 *  False for ungrouped, not-Steam-owned games and for a duplicate
 *  group's primary. */
export function isHiddenDuplicate(appId: number): boolean {
  return nonPrimaryDuplicateAppIds.has(appId);
}

/** Gate for every native-tab duplicate hide — "All Games", "Great on
 *  Deck", and "Installed" alike. Reads the setting live (no caching) so
 *  flipping it in Settings takes effect on the very next filter pass,
 *  not just after a `tabManager.rebuildTabs()` round-trip. */
function hideAsDuplicate(appId: number): boolean {
  return isGroupDuplicatesEnabled() && isHiddenDuplicate(appId);
}

/** Same precedence `game-grouping.ts`'s `pickPrimary` uses for the (today
 *  unmounted) `GameGrid` component — installed copy first, else first
 *  store in `STORE_PRIORITY` — kept in sync so both surfaces agree on
 *  which store "wins" a duplicate group if `GameGrid` is ever wired up. */
function pickGroupPrimary(
  candidates: UnifideckGameInput[],
): UnifideckGameInput {
  const installed = candidates.find((c) => c.isInstalled);
  if (installed) return installed;
  for (const store of STORE_PRIORITY) {
    const match = candidates.find((c) => c.store === store);
    if (match) return match;
  }
  return candidates[0];
}

/** Reverse index: ``"<store>:<store_game_id>"`` → shortcut appId.
 *  Lets callers that only hold a store/game-id pair (e.g. the
 *  downloads queue) resolve back to the Steam shortcut appId for
 *  launch. Populated alongside ``unifideckGameCache``. */
export const unifideckAppIdByStoreGame: Map<string, number> = new Map();

/** Resolve a Steam shortcut appId from a store + store-game-id
 *  pair. Returns null when the game isn't in the cache yet. */
export function resolveAppIdFromStoreGame(
  store: string,
  gameId: string,
): number | null {
  return unifideckAppIdByStoreGame.get(`${store}:${gameId}`) ?? null;
}

/** Version bumped on every per-app status change — patchers read
 *  this to know when to remount React subtrees. */
export const gameStateVersion: Map<number, number> = new Map();

/** AppIds of non-Unifideck shortcuts whose Exe path is valid.
 *  Anything else under `app_type === NON_STEAM_APP_TYPE` is a
 *  broken shortcut and excluded from "Installed". */
export const validThirdPartyCache: Set<number> = new Set();

type ForceRefreshCallback = (appId: number) => Promise<void>;
let forceRefreshCallback: ForceRefreshCallback | null = null;

export function setForceRefreshCallback(cb: ForceRefreshCallback | null): void {
  forceRefreshCallback = cb;
}

function variantIds(appId: number): number[] {
  const ids = new Set<number>([appId]);
  if (appId < 0) ids.add(appId + 0x100000000);
  if (appId >= 0 && appId > 0x7fffffff) ids.add(appId - 0x100000000);
  return [...ids];
}

/** True if ``a`` and ``b`` are the same shortcut appId, allowing for
 *  Steam reporting it signed on one surface and unsigned on another
 *  (see {@link variantIds}). The detail-page store switcher's "which
 *  copy am I looking at" check needs this — comparing the raw values
 *  directly silently never matches when the currently-rendered
 *  overview's appid and the cached sibling's appid come from different
 *  surfaces. */
export function appIdsMatch(a: number, b: number): boolean {
  if (a === b) return true;
  return variantIds(a).includes(b) || variantIds(b).includes(a);
}

export interface UnifideckGameInput {
  appId: number;
  store: Exclude<StoreSlug, "steam">;
  isInstalled: boolean;
  steamAppId?: number;
  storeGameId?: string;
  title?: string;
  dedupeGroupId?: string;
  editionLabel?: string;
  steamOwnedAppId?: number;
  steamOwnedEditionLabel?: string;
}

export function updateUnifideckCache(games: UnifideckGameInput[]): void {
  unifideckGameCache.clear();
  unifideckAppIdByStoreGame.clear();
  dedupeGroupSiblings.clear();
  nonPrimaryDuplicateAppIds.clear();
  steamOwnedReverseAppId.clear();
  const groupBuilders: Map<string, GroupSibling[]> = new Map();
  const groupMembers: Map<string, UnifideckGameInput[]> = new Map();
  for (const g of games) {
    const entry: UnifideckCacheEntry = {
      store: g.store,
      isInstalled: g.isInstalled,
      steamAppId: g.steamAppId,
      storeGameId: g.storeGameId,
      title: g.title,
      dedupeGroupId: g.dedupeGroupId,
      editionLabel: g.editionLabel,
      steamOwnedAppId: g.steamOwnedAppId,
      steamOwnedEditionLabel: g.steamOwnedEditionLabel,
    };
    if (g.steamOwnedAppId) {
      for (const id of variantIds(g.appId)) nonPrimaryDuplicateAppIds.add(id);
      steamOwnedReverseAppId.set(g.steamOwnedAppId, g.appId);
    }
    for (const id of variantIds(g.appId)) unifideckGameCache.set(id, entry);
    if (g.storeGameId) {
      unifideckAppIdByStoreGame.set(`${g.store}:${g.storeGameId}`, g.appId);
    }
    if (g.dedupeGroupId) {
      const siblings = groupBuilders.get(g.dedupeGroupId) ?? [];
      siblings.push({
        appId: g.appId,
        store: g.store,
        title: g.title ?? "",
        editionLabel: g.editionLabel,
      });
      groupBuilders.set(g.dedupeGroupId, siblings);

      const members = groupMembers.get(g.dedupeGroupId) ?? [];
      members.push(g);
      groupMembers.set(g.dedupeGroupId, members);
    }
  }
  for (const [groupId, siblings] of groupBuilders) {
    if (siblings.length > 1) dedupeGroupSiblings.set(groupId, siblings);
  }
  for (const members of groupMembers.values()) {
    if (members.length < 2) continue;
    // Steam cross-reference is matched per-game and isn't guaranteed to
    // land on every title-matched sibling identically (see
    // getGroupSiblings) — if ANY member found a Steam-owned match, the
    // whole group is redundant with the native Steam tile, so every
    // member is hidden rather than just the non-primary ones.
    if (members.some((m) => m.steamOwnedAppId)) {
      for (const member of members) {
        for (const id of variantIds(member.appId))
          nonPrimaryDuplicateAppIds.add(id);
      }
      continue;
    }
    const primary = pickGroupPrimary(members);
    for (const member of members) {
      if (member.appId === primary.appId) continue;
      for (const id of variantIds(member.appId))
        nonPrimaryDuplicateAppIds.add(id);
    }
  }
}

export function updateSingleGameStatus(g: UnifideckGameInput): void {
  const existing = unifideckGameCache.get(g.appId);
  const entry: UnifideckCacheEntry = {
    store: g.store,
    isInstalled: g.isInstalled,
    steamAppId: existing?.steamAppId ?? g.steamAppId,
    storeGameId: g.storeGameId ?? existing?.storeGameId,
    title: existing?.title ?? g.title,
    dedupeGroupId: existing?.dedupeGroupId ?? g.dedupeGroupId,
    editionLabel: existing?.editionLabel ?? g.editionLabel,
    steamOwnedAppId: existing?.steamOwnedAppId ?? g.steamOwnedAppId,
    steamOwnedEditionLabel:
      existing?.steamOwnedEditionLabel ?? g.steamOwnedEditionLabel,
  };
  for (const id of variantIds(g.appId)) unifideckGameCache.set(id, entry);
  if (
    existing &&
    existing.isInstalled === g.isInstalled &&
    existing.store === g.store
  )
    return;
  const next = (gameStateVersion.get(g.appId) ?? 0) + 1;
  for (const id of variantIds(g.appId)) gameStateVersion.set(id, next);
  if (forceRefreshCallback) {
    void forceRefreshCallback(g.appId).catch((e) =>
      console.error("[Unifideck] force-refresh callback failed", e),
    );
  }
  window.dispatchEvent(
    new CustomEvent("unifideck-game-state-changed", {
      detail: { appId: g.appId, isInstalled: g.isInstalled, store: g.store },
    }),
  );
}

export function updateValidThirdPartyCache(appIds: number[]): void {
  validThirdPartyCache.clear();
  for (const id of appIds) {
    for (const v of variantIds(id)) validThirdPartyCache.add(v);
  }
}

export function isUnifideckGame(appId: number): boolean {
  return unifideckGameCache.has(appId);
}

/** Resolve a shortcut's ``{ store, storeGameId, isInstalled }`` from its
 *  Steam appId, or ``null`` if it isn't a known Unifideck shortcut. Used by
 *  the "Change executable" context-menu item to gate + address the RPCs. */
export function getUnifideckGame(appId: number): {
  store: Exclude<StoreSlug, "steam">;
  storeGameId: string | undefined;
  isInstalled: boolean;
} | null {
  const cached = unifideckGameCache.get(appId);
  if (!cached) return null;
  return {
    store: cached.store,
    storeGameId: cached.storeGameId,
    isInstalled: cached.isInstalled,
  };
}

export function getStoreForApp(
  appId: number,
  appType: number,
): StoreSlug | null {
  const cached = unifideckGameCache.get(appId);
  if (cached) return cached.store;
  if (appType === NON_STEAM_APP_TYPE) return null;
  return "steam";
}

export function getInstalledStatus(
  appId: number,
  _appType: number,
  steamInstalledFlag: boolean,
): boolean {
  return unifideckGameCache.get(appId)?.isInstalled ?? steamInstalledFlag;
}

type FilterFn<K extends FilterType> = (
  params: FilterParams[K],
  app: SteamAppOverview,
) => boolean;

const filterFunctions: { [K in FilterType]: FilterFn<K> } = {
  all: (_p, app) => {
    if (app.app_type !== NON_STEAM_APP_TYPE) return true;
    if (!isUnifideckGame(app.appid)) return false;
    // Cross-store duplicates: only the chosen primary's tile shows here —
    // see nonPrimaryDuplicateAppIds — but only when the user opted into
    // grouping (default off: separate items, one tile per store). Steam's
    // native tile can't render a multi-store badge cluster, so this is a
    // hide, not a merge; the detail-page store switcher covers switching
    // to the hidden stores regardless of the setting.
    if (hideAsDuplicate(app.appid)) return false;
    return true;
  },
  installed: (params, app) => {
    if (hideAsDuplicate(app.appid)) return false;
    const isInstalled = getInstalledStatus(
      app.appid,
      app.app_type,
      app.installed,
    );
    const match = params.installed ? isInstalled : !isInstalled;
    if (params.installed && app.app_type === NON_STEAM_APP_TYPE) {
      if (unifideckGameCache.has(app.appid)) {
        return match;
      }
      return false;
    }
    return match;
  },
  platform: (params, app) => {
    if (params.platform === "all") return true;
    if (params.platform === "steam") return app.app_type !== NON_STEAM_APP_TYPE;
    return app.app_type === NON_STEAM_APP_TYPE;
  },
  store: (params, app) => {
    if (params.store === "all") return true;
    const store = getStoreForApp(app.appid, app.app_type);
    if (store === null) return false;
    return store === params.store;
  },
  deckCompat: (_p, app) => {
    if (hideAsDuplicate(app.appid)) return false;
    // Read the bits for the device actually running. Steam packs a
    // separate rating per device, and on a Machine the Deck's bits are
    // not the ones its own filters and badges use.
    //
    // The threshold has to be per-track, not a hardcoded 3: the SteamOS
    // enum's best value is 2, so comparing it against 3 made this fast
    // path unreachable and dropped every Valve-rated native Steam game
    // out of the tab on non-Deck SteamOS hardware.
    const track = activeCompatTrack();
    if (isTopRated(overviewCompatCategory(app, track), track)) {
      return true;
    }
    const cached = unifideckGameCache.get(app.appid);
    if (cached) {
      // Prefer the shortcut-keyed facet compat — the backend already
      // resolved this shortcut → real-Steam-AppID → compat via the
      // centralised title matcher, so no fuzzy lookup against the
      // lossy ``display_name`` is needed here.
      const facetCompat = getCompatByShortcutAppId(app.appid);
      if (facetCompat) return meetsGreatOnCurrentDevice(facetCompat);
      // Fallback: title-keyed compat for shortcuts the metadata phase
      // never mapped to a Steam AppID (no facet yet).
      const title = app.display_name || "";
      if (!title) return false;
      return meetsGreatOnCurrentDevice(getCachedCompatByTitle(title));
    }
    // Native Steam game (not a Unifideck shortcut): ``app.appid`` is a
    // real Steam AppID, so the appid-keyed ProtonDB rating applies.
    const tier = getCachedRating(app.appid);
    return tier === "native" || tier === "platinum";
  },
  nonSteam: (_p, app) => {
    if (app.app_type !== NON_STEAM_APP_TYPE) return false;
    if (unifideckGameCache.has(app.appid)) return false;
    return true;
  },
};

function getHiddenAppIds(): Set<number> {
  try {
    const cs = (
      window as unknown as {
        collectionStore?: {
          GetCollection?: (
            id: string,
          ) => { allApps?: Array<{ appid: number }> } | null;
        };
      }
    ).collectionStore;
    const hidden = cs?.GetCollection?.("hidden");
    if (hidden?.allApps) return new Set(hidden.allApps.map((a) => a.appid));
  } catch (e) {
    console.error("[Unifideck] hidden collection lookup failed", e);
  }
  return new Set();
}

export function isGameHidden(appId: number): boolean {
  return getHiddenAppIds().has(appId);
}

export function runFilter<T extends FilterType>(
  filter: TabFilter<T>,
  app: SteamAppOverview,
): boolean {
  const fn = filterFunctions[filter.type] as FilterFn<T> | undefined;
  if (!fn) return true;
  return fn(filter.params, app);
}

export function runFilters(
  filters: TabFilter[],
  app: SteamAppOverview,
): boolean {
  if (isGameHidden(app.appid)) return false;
  return filters.every((f) => runFilter(f, app));
}

// Shape of one row from the ``get_all_unifideck_games`` RPC —
// matches Python's ``asdict(Game)``. The frontend ``Game``
// interface in ``types/api.ts`` is misaligned (it expects
// ``is_installed`` / ``executable``); ignore it and trust the
// actual serialised field names from the backend.
interface RpcGameRow {
  app_id?: number | null;
  store?: StoreSlug;
  store_game_id?: string;
  title?: string;
  installed?: boolean;
  metadata?: Record<string, unknown>;
  dedupe_group_id?: string | null;
  edition_label?: string | null;
  steam_owned_app_id?: number | null;
  steam_owned_edition_label?: string | null;
}

type StoreCounts = Partial<Record<Exclude<StoreSlug, "steam">, number>>;
type StoreCountSink = (counts: StoreCounts) => void;

let storeCountSink: StoreCountSink | null = null;
let cacheLoadStarted = false;
let cacheLoaded = false;
// Bounded auto-retry for a failed initial load. A transient
// ``get_all_unifideck_games`` failure used to latch ``cacheLoaded =
// true`` with an empty cache, so every Unifideck shortcut failed
// ``isUnifideckGame`` and all library tabs showed 0 until the next
// full sync (UD-008 / UD-043 "synced but nothing shows"). We now only
// mark the cache authoritative on success and retry a handful of times
// with backoff, then give up (so the user's own shortcuts' native UI
// isn't hidden forever if the backend is genuinely down).
const _CACHE_RETRY_MAX = 5;
const _CACHE_RETRY_BASE_MS = 1500;
let cacheRetryCount = 0;
let cacheRetryTimer: ReturnType<typeof setTimeout> | null = null;

/** Whether the first ``loadUnifideckCache`` attempt has completed.
 *  Synchronous callers (e.g. the App-Details patch) stay optimistic
 *  before this flips so a Unifideck game opened on cold boot isn't
 *  mistaken for a plain shortcut; once true, ``isUnifideckGame`` is
 *  authoritative. */
export function isUnifideckCacheLoaded(): boolean {
  return cacheLoaded;
}

/** Register a callback to receive per-store game counts. The
 *  tab manager uses this to drive ``shouldShowTab``. */
export function setStoreCountSink(sink: StoreCountSink | null): void {
  storeCountSink = sink;
}

function isNonSteamStore(
  s: StoreSlug | undefined,
): s is Exclude<StoreSlug, "steam"> {
  return s !== undefined && s !== "steam";
}

/** Fetch the unified game list from the backend and populate
 *  ``unifideckGameCache``. Idempotent — safe to call from both
 *  the eager plugin-init path and the QAM-mount path. */
export async function loadUnifideckCache(): Promise<void> {
  try {
    const raw = await call<[], unknown>("get_all_unifideck_games");
    // Backend wraps every response in ``{success, error, data}``
    // — unwrap so we end up with the actual list of games.
    const games = unwrapRpcEnvelope<RpcGameRow[] | null | undefined>(raw, {
      route: "get_all_unifideck_games",
    });
    const inputs: UnifideckGameInput[] = [];
    const counts: Record<Exclude<StoreSlug, "steam">, number> = {
      epic: 0,
      gog: 0,
      amazon: 0,
      ubisoft: 0,
      battlenet: 0,
      microsoft: 0,
      gamevault: 0,
      itch: 0,
    };
    for (const g of games ?? []) {
      if (g.app_id == null) continue;
      if (!isNonSteamStore(g.store)) continue;
      const steamAppId =
        typeof g.metadata?.steam_app_id === "number"
          ? (g.metadata.steam_app_id as number)
          : undefined;
      inputs.push({
        appId: g.app_id,
        store: g.store,
        isInstalled: Boolean(g.installed),
        steamAppId,
        storeGameId: g.store_game_id,
        title: g.title,
        dedupeGroupId: g.dedupe_group_id ?? undefined,
        editionLabel: g.edition_label ?? undefined,
        steamOwnedAppId: g.steam_owned_app_id ?? undefined,
        steamOwnedEditionLabel: g.steam_owned_edition_label ?? undefined,
      });
      counts[g.store] += 1;
    }
    // SUCCESS: populate + mark authoritative. An empty-but-valid
    // response (0 games) legitimately marks the cache loaded — the
    // library is genuinely empty. Only an RPC *failure* must not latch.
    updateUnifideckCache(inputs);
    storeCountSink?.(counts);
    cacheLoaded = true;
    cacheRetryCount = 0;
    if (cacheRetryTimer !== null) {
      clearTimeout(cacheRetryTimer);
      cacheRetryTimer = null;
    }
  } catch (e) {
    // FAILURE: do NOT latch ``cacheLoaded`` or wipe the existing cache
    // (``updateUnifideckCache`` was never reached, so a prior good load
    // survives). Latching true here with an empty cache made every
    // library tab render 0 (UD-008/UD-043). Retry with backoff so a
    // transient failure self-heals without waiting for a full sync.
    console.error("[Unifideck] loadUnifideckCache failed", e);
    if (cacheRetryCount < _CACHE_RETRY_MAX) {
      cacheRetryCount += 1;
      const delay = _CACHE_RETRY_BASE_MS * cacheRetryCount;
      if (cacheRetryTimer !== null) clearTimeout(cacheRetryTimer);
      cacheRetryTimer = setTimeout(() => {
        cacheRetryTimer = null;
        void loadUnifideckCache();
      }, delay);
    } else {
      // Exhausted retries — accept the empty state so synchronous
      // callers stop treating every shortcut as a potential Unifideck
      // game (which would keep the user's own shortcuts' native UI
      // hidden). The next ``unifideck-sync-completed`` still retries.
      cacheLoaded = true;
    }
  }
}

/** Kick off the eager load + subscribe to sync-completed events
 *  for refresh. Idempotent — calls after the first are no-ops.
 *
 *  Returns a disposer that unregisters both subscriptions and clears any
 *  pending retry, then releases the started-guard. It used to return
 *  nothing, so the window listener and the EventBus subscription survived
 *  plugin unload; because ``EventBusClient`` only stops polling once its
 *  subscriber set empties, that alone kept a 2s ``subscribe_replay`` loop
 *  alive for the life of the Steam UI process, once per plugin reload. */
export function startUnifideckCacheAutoload(): () => void {
  if (cacheLoadStarted) return () => {};
  cacheLoadStarted = true;
  void loadUnifideckCache();
  // Eager-load the compat cache + per-shortcut facet enrichment at
  // plugin init (not just when the QAM panel mounts) so the Steam
  // library's Great-on-Deck tab + native Sort/Filters have data on
  // first render in Gaming Mode, where the panel is never opened.
  void loadCompatCacheFromBackend();
  void loadFacets();
  const onSyncCompleted = (): void => {
    // A fresh sync is a new chance for a previously-failed load —
    // reset the retry budget so a transient earlier failure doesn't
    // leave us out of retries.
    cacheRetryCount = 0;
    void loadUnifideckCache();
    // A fresh sync rebuilt the metadata/compat caches — refresh the
    // derived compat + facet data so badges/sort/filters reflect it.
    void loadCompatCacheFromBackend(true);
    void loadFacets(true);
  };
  window.addEventListener("unifideck-sync-completed", onSyncCompleted);
  // ShortcutService emits SHORTCUT_INSTALL_STATE_CHANGED on
  // post-install/uninstall — flip the per-app entry immediately so
  // the GOG tab and detail-page UI react without waiting for the
  // next full library reload.
  const unsubscribeInstallState = EventBusClient.subscribe(
    Events.SHORTCUT_INSTALL_STATE_CHANGED,
    (kw) => {
      const appId = kw.app_id;
      const store = kw.store;
      const installed = kw.installed;
      if (typeof appId !== "number") return;
      if (typeof store !== "string") return;
      if (typeof installed !== "boolean") return;
      if (!isNonSteamStore(store as StoreSlug)) return;
      updateSingleGameStatus({
        appId,
        store: store as Exclude<StoreSlug, "steam">,
        isInstalled: installed,
      });
      // The bytes on disk just changed by an entire game. Every cached size
      // for this app is now wrong in one direction or the other.
      invalidateGameSize(appId);
    },
  );
  return () => {
    window.removeEventListener("unifideck-sync-completed", onSyncCompleted);
    unsubscribeInstallState();
    // A retry armed before unload would otherwise fire into a torn-down
    // plugin and re-populate the module caches.
    if (cacheRetryTimer != null) {
      clearTimeout(cacheRetryTimer);
      cacheRetryTimer = null;
    }
    // Release the guard so a re-loaded bundle starts cleanly. The guard is
    // module-scoped, so a fresh module instance resets it anyway; this
    // matters for the same-instance reload path.
    cacheLoadStarted = false;
  };
}
