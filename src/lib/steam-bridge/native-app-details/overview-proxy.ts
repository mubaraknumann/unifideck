/**
 * A view of a shortcut's AppOverview whose display status is ours.
 *
 * Steam's action button picks its label, icon, tone (green Play, disabled,
 * …) and its Resume / ✕ split from one value:
 * `overview.selected_per_client_data.display_status`. Handing the button a
 * Proxy that answers that one read with the status of OUR state gives the
 * exact native button without writing anything to the shared overview,
 * which every other Steam surface also reads.
 *
 * Content only, never identity: `appid`, `GetGameID()`, `BIsModOrShortcut()`
 * and everything else still come from the real object (verified live: the
 * proxied overview resolves Play / Install / Update / Cancel for statuses
 * 11 / 9 / 20 / 1 and still reports the real 64-bit game id).
 */

/** Steam's live statuses. While Steam sets one of these, its own button
 *  (Cancel launch, Resume + ✕, Terminating) stays fully native. */
export const LIVE_DISPLAY_STATUSES: ReadonlySet<number> = new Set([1, 4, 36]);

/** The display statuses our states map to (values read from Steam's
 *  `EDisplayStatus` at runtime, 2026-11-02). */
export const DisplayStatus = {
  /** "Cancel": a download of ours is running. */
  Cancel: 1,
  /** "Install". */
  Install: 9,
  /** "Play", green. */
  Play: 11,
  /** Disabled "Install" with no spinner: cannot install on this device. */
  InstallUnavailable: 14,
  /** "Update". */
  Update: 20,
  /** Disabled "Play" with no spinner: installed but cannot run. */
  PlayUnavailable: 37,
} as const;

interface PerClientData {
  display_status?: number;
  status_percentage?: number;
}

const byStatus = new WeakMap<object, Map<number, object>>();
const pcdByStatus = new WeakMap<object, Map<number, object>>();
/** Status proxy → the real per-client data behind it. */
const realPcd = new WeakMap<object, PerClientData>();

/** True while Steam itself is driving the app (launching, running,
 *  terminating). The real value is read through the observable, so a
 *  MobX observer rendering through the proxy re-renders the moment Steam
 *  changes it, with no polling on our side. */
function isLive(pcd: PerClientData): boolean {
  const real = pcd.display_status;
  return typeof real === "number" && LIVE_DISPLAY_STATUSES.has(real);
}

function withStatusPcd(pcd: unknown, status: number): unknown {
  if (!pcd || typeof pcd !== "object") return pcd;
  // Steam's `selected_per_client_data` is a getter. It runs with the overview
  // proxy as `this`, so it reads the proxied `per_client_data` and hands us a
  // row that is already a proxy. Never wrap a proxy: the real row must stay
  // one step away, or the real status cannot be read.
  const real = realPcd.get(pcd) ?? (pcd as PerClientData);
  let m = pcdByStatus.get(real);
  if (!m) {
    m = new Map();
    pcdByStatus.set(real, m);
  }
  let p = m.get(status);
  if (!p) {
    p = new Proxy(real, {
      get(target, key, receiver) {
        if (key === "display_status") {
          return isLive(target) ? target.display_status : status;
        }
        // Our enrichment stamps 100 on installed rows; under any status but
        // Play it would print "100% Complete" beside the button.
        if (key === "status_percentage" && !isLive(target)) return undefined;
        return Reflect.get(target, key, receiver);
      },
    });
    realPcd.set(p, real);
    m.set(status, p);
  }
  return p;
}

/**
 * Whether Steam is driving this overview right now (see {@link isLive}).
 *
 * Reads the real status, not the proxy's answer: our Cancel is status 1, which
 * is also one of Steam's live statuses. Read through the proxy, a download of
 * ours looks like Steam launching the game, and the click goes to Steam.
 */
export function isOverviewLive(overview: unknown): boolean {
  const pcd = (overview as { selected_per_client_data?: PerClientData } | null)
    ?.selected_per_client_data;
  return !!pcd && isLive(realPcd.get(pcd) ?? pcd);
}

/**
 * The same overview, answering `display_status` with `status`. Cached per
 * (overview, status) so a re-render with the same state hands React the
 * same object and the button does not re-render for nothing.
 */
export function withDisplayStatus<T extends object>(
  overview: T,
  status: number,
): T {
  let m = byStatus.get(overview);
  if (!m) {
    m = new Map();
    byStatus.set(overview, m);
  }
  const hit = m.get(status);
  if (hit) return hit as T;
  const proxy = new Proxy(overview, {
    get(target, key, receiver) {
      if (key === "selected_per_client_data") {
        return withStatusPcd(Reflect.get(target, key, receiver), status);
      }
      if (key === "per_client_data") {
        const list = Reflect.get(target, key, receiver);
        return Array.isArray(list)
          ? list.map((pcd) => withStatusPcd(pcd, status))
          : list;
      }
      if (key === "display_status") {
        const real = Reflect.get(target, key, receiver);
        return typeof real === "number" && LIVE_DISPLAY_STATUSES.has(real)
          ? real
          : status;
      }
      return Reflect.get(target, key, receiver);
    },
  });
  m.set(status, proxy);
  return proxy;
}

/** Steam store categories (ids read live 2026-11-02 from a Steam game whose
 *  features list showed exactly these rows). */
export const StoreCategory = {
  Achievements: 22,
  CloudSaves: 23,
} as const;

/** Categories that name a Steam service, not a fact about the game. Our
 *  enrichment copies a Steam game's categories onto every store's copy, so
 *  without this a GOG copy would claim Family Sharing or Remote Play. */
const STEAM_SERVICE_CATEGORIES: ReadonlySet<number> = new Set([
  8, // Valve Anti-Cheat
  15, // Stats
  25, // Leaderboards
  29, // Trading Cards
  30, // Workshop
  40, // SteamVR Collectibles
  41, // Remote Play on Phone
  42, // Remote Play on Tablet
  43, // Remote Play on TV
  44, // Remote Play Together
  62, // Family Sharing
]);

export interface OurFeatureFacts {
  /** Our store reports achievements for this game. */
  achievements: boolean;
  /** Our metadata says this copy has cloud saves. */
  cloudSaves: boolean;
}

const byFacts = new WeakMap<object, Map<string, object>>();

/**
 * The same overview for Steam's feature rows: Achievements and Cloud Saves
 * answer from OUR facts, Steam-service categories answer false, and the
 * rest (single-player, co-op, controller support, …) are the game's own
 * facts and pass through.
 */
export function withOurFeatures<T extends object>(
  overview: T,
  facts: OurFeatureFacts,
): T {
  let m = byFacts.get(overview);
  if (!m) {
    m = new Map();
    byFacts.set(overview, m);
  }
  const key = `${facts.achievements}|${facts.cloudSaves}`;
  const hit = m.get(key);
  if (hit) return hit as T;
  const proxy = new Proxy(overview, {
    get(target, prop, receiver) {
      if (prop !== "BHasStoreCategory")
        return Reflect.get(target, prop, receiver);
      const real = Reflect.get(target, prop, receiver) as
        | ((c: number) => boolean)
        | undefined;
      return (category: number): boolean => {
        if (category === StoreCategory.Achievements) return facts.achievements;
        if (category === StoreCategory.CloudSaves) return facts.cloudSaves;
        if (STEAM_SERVICE_CATEGORIES.has(category)) return false;
        return typeof real === "function" ? real.call(target, category) : false;
      };
    },
  });
  m.set(key, proxy);
  return proxy;
}

/** What a click on the proxied button does. */
export interface ActionSpec {
  onPrimary: () => void;
}

/** Proxy → the spec of the component that made it. `.current` is updated
 *  on every render, so the click always runs the latest handler. */
const specs = new WeakMap<object, { current: ActionSpec }>();

export function bindActionSpec(
  proxy: object,
  ref: { current: ActionSpec },
): void {
  specs.set(proxy, ref);
}

export function actionSpecFor(overview: unknown): ActionSpec | null {
  if (!overview || typeof overview !== "object") return null;
  return specs.get(overview)?.current ?? null;
}

// ── Play targets ("Play from" arrow) ──────────────────────

/** The client id of our cloud target. Any id but "0" reads as a remote
 *  client to Steam, which turns Play into "Stream from <name>". */
export const CLOUD_CLIENT_ID = "unifideck-cloud";

const byTargets = new WeakMap<object, Map<string, object>>();

/**
 * The same overview, offering a second place to play (`cloudName`), so
 * Steam draws its own "Play from" arrow on the action button. With
 * `cloudSelected` the selected client is that remote one: Steam's button
 * reads "Stream / from <cloudName>" in green, as for a game streamed from
 * another PC. Only the action button is given this overview.
 *
 * With `offerLocal` false the cloud is the only place to play (a game
 * only xCloud carries): it is selected and no second client is added, so
 * Steam shows its Stream button with no arrow.
 *
 * While Steam itself runs the game the overview is answered as-is, so
 * Steam's own Resume and ✕ show and the arrow goes away.
 */
export function withPlayTargets<T extends object>(
  overview: T,
  cloudName: string,
  cloudSelected: boolean,
  offerLocal = true,
): T {
  let m = byTargets.get(overview);
  if (!m) {
    m = new Map();
    byTargets.set(overview, m);
  }
  const selected = cloudSelected || !offerLocal;
  const key = `${cloudName}|${selected}|${offerLocal}`;
  const hit = m.get(key);
  if (hit) return hit as T;
  const cloud = {
    clientid: CLOUD_CLIENT_ID,
    client_name: cloudName,
    display_status: DisplayStatus.Play,
    status_percentage: undefined,
    installed: false,
    is_available_on_current_platform: true,
  };
  const proxy = new Proxy(overview, {
    get(target, k, receiver) {
      if (isOverviewLive(target)) return Reflect.get(target, k, receiver);
      if (k === "per_client_data" && offerLocal) {
        const list = Reflect.get(target, k, receiver);
        return Array.isArray(list) ? [...list, cloud] : [cloud];
      }
      if (selected) {
        if (k === "selected_per_client_data") return cloud;
        if (k === "display_status") return DisplayStatus.Play;
      }
      return Reflect.get(target, k, receiver);
    },
  });
  m.set(key, proxy);
  return proxy;
}

/** What the "Play from" arrow opens, per proxied overview. `.current` is
 *  updated on every render, as for {@link bindActionSpec}. */
const menus = new WeakMap<object, { current: (anchor: EventTarget) => void }>();

export function bindStreamMenu(
  proxy: object,
  ref: { current: (anchor: EventTarget) => void },
): void {
  menus.set(proxy, ref);
}

export function streamMenuFor(
  overview: unknown,
): ((anchor: EventTarget) => void) | null {
  if (!overview || typeof overview !== "object") return null;
  return menus.get(overview)?.current ?? null;
}
