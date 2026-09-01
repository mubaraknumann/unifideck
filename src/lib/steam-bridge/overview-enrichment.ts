/**
 * Overview enrichment — make Steam's NATIVE library Sort menu and
 * Library Filters work for non-Steam (Unifideck) shortcuts.
 *
 * Our custom tabs render through Steam's own grid, which sorts and
 * filters purely on `AppOverview` field values. Spoofed shortcuts have
 * those empty, so this module writes them from:
 *  - per-shortcut `FacetRecord`s (`library-facets`) — metacritic,
 *    deck-compat, store categories/tags, release date, reviews.
 *    NOT date-added: see `applyFacet` — writing it into
 *    `rt_purchased_time` hijacked Steam's Home "Recent Games" shelf;
 *  - Steam's own playtime store (`SteamClient.Apps.GetPlaytime`); and
 *  - the backend `get_game_size_bytes` RPC (on-disk size, installed).
 *
 * PERSISTENCE: Steam re-creates/re-sets a shortcut's `AppOverview`
 * (e.g. after closing its details page), which WIPES any fields we
 * wrote — the game then drops out of Great-on-Deck and loses its
 * metacritic. So, like SDH-PlayTime, we patch `appStore.m_mapApps.set`
 * to re-apply our enrichment on every overview write, and cache the
 * async-sourced values (playtime, size) so the patch can re-apply them
 * synchronously.
 *
 * Write mechanisms (verified live via CEF — plan Phase-0):
 *  - plain writable fields → assign (metacritic, rt_* dates, reviews,
 *    playtime, size);
 *  - `store_category` getter → mutate `m_setStoreCategories` (Players);
 *  - `steam_deck_compat_category` getter → write
 *    `steam_hw_compat_category_packed` (Deck-compat).
 */
import { call } from "@decky/api";
import { rpcRoutes } from "../../api/rpc-routes";
import { unwrapRpcEnvelope } from "../../api/useRPC";
import { EventBusClient } from "../../api/event-bus-client";
import { Events } from "../../types/events";
import { unifideckGameCache } from "../library-filters";
import { getFacet, loadFacets, type FacetRecord } from "../library-facets";
import {
  onGameSizeInvalidated,
  registerGameSizeCache,
} from "../game-size-cache";

interface EnrichablePerClientData {
  clientid: string;
  client_name?: string;
  display_status?: number;
  status_percentage?: number;
  is_available_on_current_platform?: boolean;
  installed?: boolean;
}

interface EnrichableOverview {
  appid: number;
  app_type: number;
  metacritic_score?: number;
  rt_original_release_date?: number;
  rt_steam_release_date?: number;
  rt_purchased_time?: number;
  review_score_with_bombs?: number;
  review_percentage_with_bombs?: number;
  steam_hw_compat_category_packed?: number;
  minutes_playtime_forever?: number;
  rt_last_time_played?: number;
  size_on_disk?: number;
  m_setStoreCategories?: Set<number>;
  m_setStoreTags?: Set<number>;
  per_client_data?: EnrichablePerClientData[];
  most_available_clientid?: string;
}

type AppMap = Map<number, EnrichableOverview> & {
  __unifideckOriginalSet?: Map<number, EnrichableOverview>["set"];
};

interface AppStoreLike {
  GetAppOverviewByAppID?: (id: number) => EnrichableOverview | null;
  m_mapApps?: AppMap;
}

interface AppsApiLike {
  GetPlaytime?: (
    appId: number,
  ) => Promise<{ nPlaytimeForever?: number; rtLastTimePlayed?: number } | null>;
}

const NON_STEAM_APP_TYPE = 1073741824;

// Async-sourced values cached so the m_mapApps.set patch can re-apply
// them synchronously when Steam re-sets an overview.
const playtimeByAppId = new Map<number, { mins: number; last: number }>();
const sizeByAppId = new Map<number, number>();
const sizeFetched = new Set<number>();

// `sizeFetched` makes each app measurable at most once per Steam session,
// which is right for a polite background trickle and wrong the moment a
// game's bytes actually change. Clearing the LATCH as well as the value is
// what matters here: `enrichSizes` short-circuits on it, so dropping only
// the number would leave `size_on_disk` permanently unset instead of stale.
registerGameSizeCache((appId) => {
  sizeByAppId.delete(appId);
  sizeFetched.delete(appId);
});

function getAppStore(): AppStoreLike | null {
  return (window as unknown as { appStore?: AppStoreLike }).appStore ?? null;
}

function getAppsApi(): AppsApiLike | null {
  return (
    (window as unknown as { SteamClient?: { Apps?: AppsApiLike } }).SteamClient
      ?.Apps ?? null
  );
}

function forEachShortcutOverview(cb: (ov: EnrichableOverview) => void): void {
  const map = getAppStore()?.m_mapApps;
  if (!map) return;
  for (const ov of map.values()) {
    if (ov && ov.app_type === NON_STEAM_APP_TYPE) cb(ov);
  }
}

/** "21 Jun, 2022" → unix seconds, or 0 when unparseable. */
function releaseUnix(dateStr: string): number {
  if (!dateStr) return 0;
  const ms = Date.parse(dateStr);
  return Number.isNaN(ms) ? 0 : Math.floor(ms / 1000);
}

/** Reconcile a MobX-backed id set to exactly `ids` (clear + add). Safe
 *  because we only call this on non-Steam shortcuts, whose sets Steam
 *  itself never populates. */
function setIds(target: Set<number> | undefined, ids: number[]): void {
  if (!target || typeof target.add !== "function") return;
  try {
    target.clear();
    for (const id of ids) target.add(id);
  } catch {
    /* not a real Set on this Steam build — skip */
  }
}

function applyFacet(ov: EnrichableOverview, facet: FacetRecord): void {
  if (typeof facet.metacritic === "number")
    ov.metacritic_score = facet.metacritic;
  const rel = releaseUnix(facet.release_date);
  if (rel > 0) {
    ov.rt_original_release_date = rel;
    ov.rt_steam_release_date = rel;
  }
  // Steam's Home "Recent Games" shelf ranks its candidates by
  //   max(rt_last_time_locally_played, rt_purchased_time,
  //       installed ? rt_last_time_played_or_installed : 0)
  // over a pool that ALREADY includes every non-Steam shortcut (the
  // LocalGames collection). Stock Steam leaves shortcuts at 0 so they
  // sort harmlessly to the bottom. Projecting our `date_added_unix`
  // into `rt_purchased_time` (to power the native "Date Added" sort)
  // gave every freshly-synced game a recency of *now* and floated the
  // whole library to the top of the shelf. Keep the field at 0.
  // Scrubbing — rather than simply not writing — also clears values a
  // previous build left on live overviews when the plugin is reloaded
  // without a Steam restart.
  if (ov.rt_purchased_time) ov.rt_purchased_time = 0;
  if (typeof facet.review_score === "number") {
    ov.review_score_with_bombs = facet.review_score;
  }
  if (typeof facet.review_percentage === "number") {
    ov.review_percentage_with_bombs = facet.review_percentage;
  }
  if (facet.deck_category > 0) {
    const cur = ov.steam_hw_compat_category_packed ?? 0;
    ov.steam_hw_compat_category_packed = (cur & ~3) | (facet.deck_category & 3);
  }
  setIds(ov.m_setStoreCategories, facet.store_category);
  setIds(ov.m_setStoreTags, facet.store_tag);
}

/** Shape Steam itself uses: installed → display_status 11 /
 *  status_percentage 100; not installed → display_status 9 /
 *  status_percentage absent. `installed` drives the getter;
 *  `display_status` drives the renderer's DOM class. */
function applyPerClientDataShape(
  entry: EnrichablePerClientData,
  isInstalled: boolean,
): void {
  entry.installed = isInstalled;
  if (isInstalled) {
    entry.display_status = 11;
    entry.status_percentage = 100;
  } else {
    entry.display_status = 9;
    delete entry.status_percentage;
  }
}

/** Set the local (`clientid==="0"`) per_client_data entry's install
 *  state. Defensively also writes whatever `most_available_clientid`
 *  resolves to in case it differs. */
function applyInstalledState(
  ov: EnrichableOverview,
  isInstalled: boolean,
): void {
  if (!ov.per_client_data) ov.per_client_data = [];

  let localEntry = ov.per_client_data.find((e) => e.clientid === "0");
  if (!localEntry) {
    localEntry = {
      clientid: "0",
      client_name: "",
      is_available_on_current_platform: true,
    };
    ov.per_client_data.push(localEntry);
  }
  applyPerClientDataShape(localEntry, isInstalled);

  // Defensive dual-write in case most_available_clientid differs from "0".
  if (ov.most_available_clientid && ov.most_available_clientid !== "0") {
    let mostAvailEntry = ov.per_client_data.find(
      (e) => e.clientid === ov.most_available_clientid,
    );
    if (!mostAvailEntry) {
      mostAvailEntry = {
        clientid: ov.most_available_clientid,
        client_name: "",
        is_available_on_current_platform: true,
      };
      ov.per_client_data.push(mostAvailEntry);
    }
    applyPerClientDataShape(mostAvailEntry, isInstalled);
  }

  // Nudge MobX/React reactivity.
  try {
    (ov as unknown as { TriggerChange?: () => void }).TriggerChange?.();
  } catch {
    /* never break Steam's own write path */
  }
}

/** Immediately correct the install-state field for one shortcut's live
 *  overview, without waiting for the next full enrichment sweep. Safe
 *  to call even if the overview isn't currently in `m_mapApps` (no-op). */
export function enrichInstalledState(
  appid: number,
  isInstalled: boolean,
): void {
  const ov = getAppStore()?.m_mapApps?.get(appid);
  if (!ov || ov.app_type !== NON_STEAM_APP_TYPE) return;
  applyInstalledState(ov, isInstalled);
}

/** Apply EVERYTHING we know for one shortcut overview (facet + cached
 *  playtime + cached size). Cheap and idempotent; used by the bulk
 *  passes AND the m_mapApps.set patch. */
function applyEnrichment(ov: EnrichableOverview): void {
  if (!ov || ov.app_type !== NON_STEAM_APP_TYPE) return;
  const facet = getFacet(ov.appid);
  if (facet) applyFacet(ov, facet);
  const pt = playtimeByAppId.get(ov.appid);
  if (pt) {
    if (pt.mins > 0) ov.minutes_playtime_forever = pt.mins;
    if (pt.last > 0) ov.rt_last_time_played = pt.last;
  }
  const sz = sizeByAppId.get(ov.appid);
  if (typeof sz === "number" && sz > 0) ov.size_on_disk = sz;
  // Also correct install state.
  const cacheEntry = unifideckGameCache.get(ov.appid);
  if (cacheEntry) {
    applyInstalledState(ov, cacheEntry.isInstalled);
  }
}

/** Re-apply enrichment to every shortcut overview currently in the map. */
export function enrichAllShortcuts(): void {
  let count = 0;
  forEachShortcutOverview((ov) => {
    if (getFacet(ov.appid)) {
      applyEnrichment(ov);
      count++;
    }
  });
  if (count > 0) {
    console.log(`[Unifideck] Enriched ${count} shortcut overviews`);
  }
}

/**
 * Pull each shortcut's playtime from Steam's own store (`GetPlaytime`)
 * — the overview's `minutes_playtime_forever` is 0 for shortcuts, but
 * Steam tracks the real value separately. Cache it (for the set-patch)
 * and write it onto the live overview.
 */
export async function enrichPlaytime(): Promise<void> {
  const apps = getAppsApi();
  if (!apps?.GetPlaytime) return;
  const overviews: EnrichableOverview[] = [];
  forEachShortcutOverview((ov) => overviews.push(ov));
  let count = 0;
  const CHUNK = 25;
  for (let i = 0; i < overviews.length; i += CHUNK) {
    await Promise.all(
      overviews.slice(i, i + CHUNK).map(async (ov) => {
        try {
          const r = await apps.GetPlaytime!(ov.appid);
          if (!r) return;
          const mins =
            typeof r.nPlaytimeForever === "number" ? r.nPlaytimeForever : 0;
          const last =
            typeof r.rtLastTimePlayed === "number" ? r.rtLastTimePlayed : 0;
          if (mins > 0 || last > 0) {
            playtimeByAppId.set(ov.appid, { mins, last });
            if (mins > 0) ov.minutes_playtime_forever = mins;
            if (last > 0) ov.rt_last_time_played = last;
            count++;
          }
        } catch {
          /* skip this app */
        }
      }),
    );
  }
  if (count > 0) {
    console.log(`[Unifideck] Enriched playtime for ${count} shortcuts`);
  }
}

/**
 * Populate `size_on_disk` for INSTALLED shortcuts via the backend
 * `get_game_size_bytes` RPC. The RPC can shell out, so this is a
 * polite one-shot background trickle (each game fetched at most once
 * per session; results cached for the set-patch).
 */
export async function enrichSizes(): Promise<void> {
  const targets: EnrichableOverview[] = [];
  forEachShortcutOverview((ov) => {
    if (sizeFetched.has(ov.appid)) return;
    if (!unifideckGameCache.get(ov.appid)?.isInstalled) return;
    targets.push(ov);
  });
  for (const ov of targets) {
    sizeFetched.add(ov.appid);
    try {
      const raw = await call<[number], unknown>(
        rpcRoutes.getGameSizeBytes,
        ov.appid,
      );
      const bytes = unwrapRpcEnvelope<number>(raw, {
        route: rpcRoutes.getGameSizeBytes,
      });
      if (typeof bytes === "number" && bytes > 0) {
        sizeByAppId.set(ov.appid, bytes);
        ov.size_on_disk = bytes;
      }
    } catch {
      /* leave size unset for this game */
    }
    await new Promise((r) => setTimeout(r, 120));
  }
}

/**
 * Patch `appStore.m_mapApps.set` so our enrichment is re-applied every
 * time Steam writes a shortcut's overview — otherwise closing a game's
 * details page (which re-sets the overview) wipes our fields and the
 * game drops out of Great-on-Deck / loses its metacritic. Mirrors
 * SDH-PlayTime's `steamPlayTimePatches`. Returns a disposer.
 */
function patchAppMapSet(): () => void {
  const map = getAppStore()?.m_mapApps;
  if (!map || typeof map.set !== "function" || map.__unifideckOriginalSet) {
    return () => {};
  }
  const originalSet = map.set.bind(map) as Map<
    number,
    EnrichableOverview
  >["set"];
  map.__unifideckOriginalSet = originalSet;
  map.set = (appId: number, overview: EnrichableOverview) => {
    try {
      applyEnrichment(overview);
    } catch {
      /* never break Steam's own write path */
    }
    return originalSet(appId, overview);
  };
  return () => {
    if (map.__unifideckOriginalSet) {
      map.set = map.__unifideckOriginalSet;
      delete map.__unifideckOriginalSet;
    }
  };
}

let started = false;

/**
 * Boot entry — load facets, enrich, install the persistence patch, and
 * keep overviews fresh. Invoked from `definePlugin` (boot), NOT the QAM
 * panel, so the native Sort/Filters work in Gaming Mode.
 */
export function startOverviewEnrichment(): () => void {
  if (started) return () => {};
  started = true;

  let unpatchMap: () => void = () => {};

  const run = (): void => {
    enrichAllShortcuts();
    void enrichPlaytime();
    void enrichSizes();
  };

  const runWhenReady = (attempt = 0): void => {
    const appStore = getAppStore();
    const ready = !!appStore?.m_mapApps && appStore.m_mapApps.size > 0;
    if (!ready && attempt < 15) {
      setTimeout(() => runWhenReady(attempt + 1), 1000);
      return;
    }
    unpatchMap = patchAppMapSet();
    run();
  };

  void loadFacets().then(() => runWhenReady());

  const onSync = (): void => {
    void loadFacets(true).then(run);
    setTimeout(() => {
      void loadFacets(true).then(run);
    }, 8000);
  };
  const onState = (): void => run();
  window.addEventListener("unifideck-sync-completed", onSync);
  window.addEventListener("unifideck-game-state-changed", onState);

  // Re-measure an app whose size was invalidated. Debounced because an
  // install completing fires more than one signal (download-complete and
  // the shortcut install-state flip), and `enrichSizes` walks every
  // shortcut — one pass covers however many were invalidated together.
  let resizeTimer: ReturnType<typeof setTimeout> | undefined;
  const unsubSize = onGameSizeInvalidated(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      void enrichSizes();
    }, 1000);
  });

  const unsubStop = EventBusClient.subscribe(Events.GAME_STOPPED, () => {
    setTimeout(() => {
      void enrichPlaytime();
    }, 3000);
  });

  // The metacritic backfill finishes in the background AFTER the sync's
  // progress bar hits 100% (and after `onSync`'s short retry window), so
  // its long-tail scores would otherwise only appear on the next
  // resync/restart. Re-read facets + re-enrich when it signals done.
  const unsubBackfill = EventBusClient.subscribe(
    Events.METADATA_BACKFILL_COMPLETE,
    () => {
      void loadFacets(true).then(run);
    },
  );

  return () => {
    window.removeEventListener("unifideck-sync-completed", onSync);
    window.removeEventListener("unifideck-game-state-changed", onState);
    clearTimeout(resizeTimer);
    try {
      unsubSize();
    } catch {
      /* ignore */
    }
    try {
      unsubStop?.();
    } catch {
      /* ignore */
    }
    try {
      unsubBackfill?.();
    } catch {
      /* ignore */
    }
    try {
      unpatchMap();
    } catch {
      /* ignore */
    }
  };
}
