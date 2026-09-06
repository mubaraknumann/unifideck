/**
 * Steam Store spoofing for non-Steam Unifideck shortcuts.
 *
 * Ported from `staging:src/spoofing/SteamStorePatcher.ts`.
 *
 * Strategy: monkey-patch three Steam getter methods so that a Unifideck
 * shortcut (synthetic AppID) is presented with the real Steam Store
 * *content* for its matched real-Steam-AppID. The mappings and rich
 * `appdetails` JSON are pre-fetched by the backend `MetadataService`
 * during sync and read here via two RPCs.
 *
 *   - `get_real_steam_appid_mappings` → `{shortcut_id: real_id}`
 *   - `get_steam_metadata_cache`      → `{real_id: appdetails}`
 *
 * **Borrow content, never identity.** These getters must always answer
 * with the shortcut's OWN `appid` / `GameID()` / `unAppID`. They used to
 * return the matched Steam app's overview and details objects outright,
 * which made Steam resolve the shortcut to that app and launch it under
 * that app's id — see the comment on `GetAppOverviewByAppID` below for the
 * bundle evidence. Metadata now reaches the UI via
 * `injectMetadataIntoOverview` / `borrowDetails`, which copy fields onto
 * our own objects.
 *
 * Net effect: Epic / GOG / Amazon / Ubisoft shortcuts in Steam's
 * library show real cover art, store descriptions, Metacritic
 * scores, etc. Launch behaviour is unchanged — clicking Play
 * still routes through the GameAction interceptor to the actual
 * launcher.
 */
import { call } from "@decky/api";
import { rpcRoutes } from "../../api/rpc-routes";
import { toSignedAppId, toUnsignedAppId } from "./appid";

let steamAppIdMappings: Record<number, number> = {};
let appDetailsCache: Record<number, AppDetails> = {};
const patchedOverviews = new Set<number>();

/** Resolves once `loadFromBackend()` has completed (mappings +
 *  metadata cache populated). `reinjectMetadataWhenLoaded` awaits this
 *  so that navigation to a game page before the async init has
 *  finished still triggers artwork injection as soon as the data
 *  is ready. */
let _backendLoadPromise: Promise<void> | null = null;

interface BackendMappingsResponse {
  success: boolean;
  mappings: Record<string, number>;
}

interface BackendMetadataResponse {
  success: boolean;
  metadata: Record<string, AppDetailsRaw>;
}

interface AppDetailsRaw {
  name?: string;
  short_description?: string;
  detailed_description?: string;
  about_the_game?: string;
  developers?: string[];
  publishers?: string[];
  release_date?: { date?: string };
  header_image?: string;
  capsule_image?: string;
  controller_support?: "full" | "partial" | string;
  metacritic?: { score?: number; url?: string };
  recommendations?: { total?: number };
  categories?:
    | Array<{ id: number; description?: string }>
    | Record<string, number>;
  genres?: Array<{ id: number; description?: string }> | Record<string, number>;
  screenshots?: Array<{ path_full?: string }>;
  platforms?: { windows?: boolean; mac?: boolean; linux?: boolean };
  supported_languages?: string | Record<string, number>;
  achievements?: { total?: number };
  dlc?: number[];
  website?: string;
}

interface AppOverview extends Record<string, unknown> {
  appid: number;
  display_name: string;
  app_type: number;
  visible_in_game_list: boolean;
  BIsShortcut?: () => boolean;
  BIsModOrShortcut?: () => boolean;
  GameID?: () => string;
}

interface AppDetails extends Record<string, unknown> {
  unAppID: number;
  strDisplayName: string;
}

interface AppStoreLike {
  GetAppOverviewByAppID: (id: number) => AppOverview | null;
  m_mapApps?: Map<number, AppOverview>;
}

interface AppDetailsStoreLike {
  GetAppDetails: (id: number) => AppDetails | null;
  GetAppData?: (id: number) => unknown;
}

function getAppStore(): AppStoreLike | null {
  return (window as unknown as { appStore?: AppStoreLike }).appStore ?? null;
}

function getAppDetailsStore(): AppDetailsStoreLike | null {
  return (
    (window as unknown as { appDetailsStore?: AppDetailsStoreLike })
      .appDetailsStore ?? null
  );
}

/** The real Steam AppID matched to a Unifideck shortcut, or 0.
 *
 *  `shortcuts.vdf` stores the *signed* 32-bit appid while callers hand us
 *  either form, so probe both — the same signed/unsigned split the backend
 *  `get_steam_compat_tool_override` documents. */
function lookupRealId(shortcutAppId: number): number {
  return (
    steamAppIdMappings[toSignedAppId(shortcutAppId)] ??
    steamAppIdMappings[toUnsignedAppId(shortcutAppId)] ??
    steamAppIdMappings[shortcutAppId] ??
    0
  );
}

function extractIds(
  data: AppDetailsRaw["categories"] | AppDetailsRaw["genres"],
): number[] {
  if (!data) return [];
  if (Array.isArray(data)) {
    return data
      .map((x) => x.id)
      .filter((id): id is number => typeof id === "number");
  }
  return Object.keys(data)
    .filter((k) => k.startsWith("category_") || k.startsWith("genre_"))
    .map((k) => Number(k.split("_")[1]))
    .filter((n) => !Number.isNaN(n));
}

function extractLanguages(
  data: AppDetailsRaw["supported_languages"],
): Array<{ strLanguageName: string }> {
  if (!data) return [];
  if (typeof data === "string") {
    return data.split(",").map((l) => ({ strLanguageName: l.trim() }));
  }
  return Object.keys(data).map((lang) => ({
    strLanguageName: lang.charAt(0).toUpperCase() + lang.slice(1),
  }));
}

// NOTE: there used to be a `buildOverview(steamAppId, raw)` here that
// synthesised a whole `AppOverview` for the matched Steam app, cached in
// `storeDataCache` and returned from `GetAppOverviewByAppID` whenever the
// user did NOT own that app. Its `appid`, `BIsShortcut: () => false` and
// `GameID: () => String(steamAppId)` were the real Steam app's, so it leaked
// the same wrong identity as the owned-overview path — just for a different
// set of titles. Overviews are no longer substituted at all (see
// `GetAppOverviewByAppID`), so the factory and its cache are gone; the
// library-facing fields it supplied are set by `overview-enrichment.ts`,
// which writes them onto the shortcut's own overview.

function buildDetails(steamAppId: number, raw: AppDetailsRaw): AppDetails {
  const developers = raw.developers ?? [];
  const publishers = raw.publishers ?? [];
  const rtRelease = raw.release_date?.date
    ? Math.floor(new Date(raw.release_date.date).getTime() / 1000)
    : 0;
  const cloud = extractIds(raw.categories).includes(23);
  return {
    unAppID: steamAppId,
    strDisplayName: raw.name ?? "",
    strDeveloperName: developers[0] ?? "",
    strHomepageURL: raw.website ?? "",
    strDescription: raw.short_description ?? "",
    strFullDescription: raw.detailed_description ?? raw.about_the_game ?? "",
    associations: {
      rgDevelopers: developers.map((d) => ({ strName: d, strURL: "" })),
      rgPublishers: publishers.map((p) => ({ strName: p, strURL: "" })),
      rgFranchises: [],
    },
    rtReleaseDate: rtRelease,
    vecPlatforms: [
      raw.platforms?.windows && "windows",
      raw.platforms?.mac && "osx",
      raw.platforms?.linux && "linux",
    ].filter((p): p is string => Boolean(p)),
    vecLanguages: extractLanguages(raw.supported_languages),
    bCloudAvailable: cloud,
    bCloudEnabledForApp: cloud,
    achievements: {
      nAchieved: 0,
      nTotal: raw.achievements?.total ?? 0,
      vecAchievedHidden: [],
      vecHighlight: [],
      vecUnachieved: [],
    },
    eSteamInputControllerMask:
      raw.controller_support === "full"
        ? 2
        : raw.controller_support === "partial"
        ? 1
        : 0,
    vecDLC: (raw.dlc ?? []).map((id) => ({
      appid: id,
      strName: "",
      bInstalled: false,
    })),
    nScreenshots: raw.screenshots?.length ?? 0,
    lDiskSpaceRequiredBytes: 0,
    vecDeckCompatTestResults: [],
    __from_web_api: true,
  };
}

/** Rich store details for the matched Steam app, re-stamped with the
 *  SHORTCUT's identity.
 *
 *  The AppDetails page genuinely wants the real store copy (description,
 *  developer, languages, DLC…), but handing Steam the matched app's object
 *  wholesale leaks its `unAppID` into every surface that reads details —
 *  the same class of bug as returning its `AppOverview`. Copy the content,
 *  keep our own id and display name. */
function borrowDetails(
  borrowed: AppDetails | null,
  shortcutAppId: number,
  own: AppDetails | null,
): AppDetails | null {
  if (!borrowed) return own;
  const merged: AppDetails = { ...borrowed, unAppID: shortcutAppId };
  const ownName = own?.strDisplayName;
  if (typeof ownName === "string" && ownName) merged.strDisplayName = ownName;
  return merged;
}

interface OverviewMutable extends AppOverview {
  TriggerChange?: () => void;
}

function injectMetadataIntoOverview(overview: OverviewMutable): boolean {
  if (!overview) return false;
  const rawAppId =
    typeof overview.appid === "function"
      ? (overview.appid as () => number)()
      : overview.appid;
  if (typeof rawAppId !== "number") return false;
  if (patchedOverviews.has(rawAppId)) return false;
  const realId = lookupRealId(rawAppId);
  if (!realId) return false;
  const details = appDetailsCache[realId];
  if (!details) return false;
  if (
    typeof details.strDeveloperName === "string" &&
    details.strDeveloperName
  ) {
    overview.developer = details.strDeveloperName;
    overview.strDeveloperName = details.strDeveloperName;
  }
  const publishers = (
    details.associations as
      | {
          rgPublishers?: Array<{ strName?: string }>;
        }
      | undefined
  )?.rgPublishers;
  if (publishers?.[0]?.strName) {
    overview.publisher = publishers[0].strName;
    overview.strPublisherName = publishers[0].strName;
  }
  if (typeof details.strDescription === "string") {
    overview.short_description = details.strDescription;
    overview.strShortDescription = details.strDescription;
  }
  if (typeof details.rtReleaseDate === "number") {
    overview.rt_original_release_date = details.rtReleaseDate;
    overview.rt_steam_release_date = details.rtReleaseDate;
  }
  patchedOverviews.add(rawAppId);
  try {
    overview.TriggerChange?.();
  } catch {
    /* ignore */
  }
  return true;
}

async function loadFromBackend(): Promise<void> {
  try {
    const mappingsResp = await call<[], BackendMappingsResponse>(
      rpcRoutes.getRealSteamAppidMappings,
    );
    if (mappingsResp?.success && mappingsResp.mappings) {
      const out: Record<number, number> = {};
      for (const [k, v] of Object.entries(mappingsResp.mappings)) {
        const key = Number(k);
        if (!Number.isNaN(key)) out[key] = v;
      }
      steamAppIdMappings = out;
    }
  } catch (e) {
    console.error("[Unifideck Store Patch] failed to load mappings:", e);
  }
  if (Object.keys(steamAppIdMappings).length === 0) return;
  try {
    const metaResp = await call<[], BackendMetadataResponse>(
      rpcRoutes.getSteamMetadataCache,
    );
    if (metaResp?.success && metaResp.metadata) {
      for (const [k, raw] of Object.entries(metaResp.metadata)) {
        const steamId = Number(k);
        if (Number.isNaN(steamId)) continue;
        appDetailsCache[steamId] = buildDetails(steamId, raw);
      }
    }
  } catch (e) {
    console.error("[Unifideck Store Patch] failed to load metadata:", e);
  }
}

/** Force a single shortcut's in-memory overview to be re-spoofed.
 *  Called from the AppDetails route patch when the user opens the
 *  details page of a Unifideck shortcut. */
export function forceInjectMetadataForShortcut(shortcutAppId: number): boolean {
  const appStore = getAppStore();
  const unsigned = toUnsignedAppId(shortcutAppId);
  patchedOverviews.delete(unsigned);
  const overview = appStore?.m_mapApps?.get(unsigned);
  if (!overview) return false;
  return injectMetadataIntoOverview(overview);
}

/** Re-spoof one shortcut's overview once the backend cache has loaded.
 *
 *  This used to end by awaiting the backend `inject_game_to_appinfo` RPC,
 *  "so the spoofing survives a Steam restart". That RPC was a stub that
 *  logged and returned `{success: true}` — its own docstring admitted the
 *  success was only there to stop the fire-and-forget call logging a failure
 *  on every navigation. The persistence it promised was redundant anyway:
 *  `applyAppStorePatch` awaits `loadFromBackend()` and re-spoofs on every
 *  plugin load, so surviving a restart is handled by re-patching rather than
 *  by writing `appinfo.vdf`. RPC, route and round-trip deleted; the local
 *  half, which does the real work, is all that is left. Audit item 35. */
export async function reinjectMetadataWhenLoaded(
  shortcutAppId: number,
): Promise<void> {
  // Wait for the async backend load to finish so that
  // steamAppIdMappings is populated. On first navigation this
  // may still be in-flight; on subsequent calls the cached
  // promise resolves immediately.
  if (_backendLoadPromise) await _backendLoadPromise;
  if (!lookupRealId(shortcutAppId)) return;
  forceInjectMetadataForShortcut(shortcutAppId);
}

interface PatchHandle {
  remove(): void;
}

/**
 * Apply the Steam-store patches. Returns a handle whose `remove()`
 * restores the original Steam getters. Call from the plugin entry
 * after `SteamBridge` is constructed.
 */
export async function applyAppStorePatch(): Promise<PatchHandle> {
  _backendLoadPromise = loadFromBackend();
  await _backendLoadPromise;
  const appStore = getAppStore();
  const appDetailsStore = getAppDetailsStore();
  if (!appStore || !appDetailsStore) {
    console.warn(
      "[Unifideck Store Patch] appStore / appDetailsStore unavailable",
    );
    return { remove: () => {} };
  }
  const origGetOverview = appStore.GetAppOverviewByAppID.bind(appStore);
  const origGetDetails = appDetailsStore.GetAppDetails.bind(appDetailsStore);
  const origGetData = appDetailsStore.GetAppData?.bind(appDetailsStore);

  appStore.GetAppOverviewByAppID = (id: number) => {
    const own = origGetOverview(id);
    if (!lookupRealId(id)) return own;
    // This getter runs on every overview read across the whole library, not
    // just on AppDetails open. Only re-spoof an id we have not already
    // patched — the previous code fired an RPC round-trip here every time.
    if (!patchedOverviews.has(toUnsignedAppId(id))) {
      void reinjectMetadataWhenLoaded(id);
    }
    // ALWAYS the shortcut's own overview. This used to return
    // `origGetOverview(realId)` — the matched Steam app's whole overview
    // object — falling back to `storeDataCache[realId]`, whose synthetic
    // `appid` / `GameID()` are also the real Steam app's. Either way Steam
    // resolved our shortcut to a different app and launched it under that
    // identity: the 2026-08-25 bundle shows Ys I (shortcut 3969905431)
    // tracked as `gameID 223810` and Trails (3057628334) as `251150`, so
    // the loading screen waited on a window for an app that never started
    // and the game ran behind it with only Abort available. Borrowed store
    // metadata reaches the UI through `injectMetadataIntoOverview`, which
    // copies fields onto THIS object and never touches identity.
    return own;
  };
  appDetailsStore.GetAppDetails = (id: number) => {
    const own = origGetDetails(id);
    const realId = lookupRealId(id);
    if (!realId) return own;
    const borrowed = origGetDetails(realId) ?? appDetailsCache[realId] ?? null;
    return borrowDetails(borrowed, id, own);
  };
  if (origGetData) {
    appDetailsStore.GetAppData = (id: number) => {
      const realId = lookupRealId(id);
      if (!realId) return origGetData(id);
      const own = origGetData(id);
      if (own) return own;
      const borrowed =
        origGetDetails(realId) ?? appDetailsCache[realId] ?? null;
      const details = borrowDetails(borrowed, id, null);
      const overview = origGetOverview(id);
      if (overview || details) return { overview, details };
      return own;
    };
  }

  console.log(
    `[Unifideck Store Patch] active — ${
      Object.keys(steamAppIdMappings).length
    } mappings, ${Object.keys(appDetailsCache).length} metadata entries`,
  );

  return {
    remove: () => {
      appStore.GetAppOverviewByAppID = origGetOverview;
      appDetailsStore.GetAppDetails = origGetDetails;
      if (origGetData) appDetailsStore.GetAppData = origGetData;
      steamAppIdMappings = {};
      appDetailsCache = {};
      patchedOverviews.clear();
    },
  };
}
