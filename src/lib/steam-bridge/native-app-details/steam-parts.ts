/**
 * Steam's own presentational parts, reused with our data.
 *
 * Each is found by shape (strings in its source, or its export's form),
 * resolved once, and `null` when this Steam build has moved it. Callers
 * drop only that one piece when a part is missing; nothing here throws.
 *
 * Verified live 2026-11-02:
 *  - Section: `AppDetailsSection` (label, highlight, children) — the
 *    wrapper every Your stuff / Activity section uses.
 *  - Feature row: `{overview, feature}` → one icon + name row of Steam's
 *    features list; decides from `overview.BHasStoreCategory(...)`.
 *  - Feature ids: the order Steam's own Game info lists them in, read from
 *    its source (`feature:<mod>.<key>`).
 *  - Compat: Steam's Deck / SteamOS category label and icon helpers.
 *  - Collections: the store, tile, grid, grid cell and scroll-container
 *    hook Steam's Game info uses for "Found in these collections".
 */
import type { ComponentType } from "react";
import {
  findExportInModule,
  getWebpackRequire,
  isClassComponent,
  isMemo,
} from "./webpack";

type AnyComponent = ComponentType<Record<string, unknown>>;

function once<T>(resolve: () => T | null): () => T | null {
  let done = false;
  let value: T | null = null;
  return () => {
    if (!done) {
      done = true;
      try {
        value = resolve();
      } catch (e) {
        console.warn("[Unifideck NativeDetails] Steam part not found:", e);
        value = null;
      }
    }
    return value;
  };
}

function isFn(v: unknown): v is (...a: unknown[]) => unknown {
  return typeof v === "function";
}

function src(v: unknown): string {
  try {
    return String(v);
  } catch {
    return "";
  }
}

/** All function exports of the first module whose source holds `needle`. */
function functionsOfModule(
  needle: string,
): Array<(...a: unknown[]) => unknown> {
  const req = getWebpackRequire();
  if (!req) return [];
  for (const id of Object.keys(req.m)) {
    if (!src(req.m[id]).includes(needle)) continue;
    let exports: Record<string, unknown>;
    try {
      exports = req(id) as Record<string, unknown>;
    } catch {
      continue;
    }
    return Object.keys(exports)
      .map((k) => {
        try {
          return exports[k];
        } catch {
          return undefined;
        }
      })
      .filter(isFn);
  }
  return [];
}

// ── Section ───────────────────────────────────────────────

export const getAppDetailsSection = once<AnyComponent>(
  () =>
    findExportInModule(
      [
        "AppDetailsSectionContainer",
        "AppDetailsSectionHasLabel",
        "RightColumnSection",
      ],
      isMemo,
    ) as unknown as AnyComponent | null,
);

// ── Achievement icon ──────────────────────────────────────

/**
 * Steam's own achievement icon, the one its App-Details carousel draws:
 * props `{achievement: {name, hidden, icon_achieved, icon_unachieved},
 * globalUnlockPercentage, unlocked, pauseAnimation, className, size}`.
 * It sizes the icon square, draws hidden ones as Steam's "?" box, and adds
 * the animated rare glow by Steam's own rule (unlocked, under 10%).
 *
 * Found by its module: the one setting `--icon-size` next to the glow
 * classes. Of its exports, the wrapper that reads `icon_achieved` and not
 * `internal_key` (that one fetches Steam's own percentages by app id).
 */
export const getAchievementIcon = once<AnyComponent>(
  () =>
    findExportInModule(
      ["RareAchievementIconGlowContainerRoot", "--icon-size"],
      (v): v is AnyComponent =>
        isFn(v) &&
        src(v).includes("icon_achieved") &&
        !src(v).includes("internal_key"),
    ) as AnyComponent | null,
);

// ── Features ──────────────────────────────────────────────

export const getFeatureRow = once<AnyComponent>(
  () =>
    findExportInModule(
      ["suppresstooltip", "sIconURL"],
      (v): v is AnyComponent =>
        isClassComponent(v) && src(v).includes("sIconURL"),
    ) as AnyComponent | null,
);

/** Steam's own feature list, in its order, as numeric feature ids. */
export const getFeatureIds = once<number[]>(() => {
  const req = getWebpackRequire();
  if (!req) return null;
  for (const id of Object.keys(req.m)) {
    const s = src(req.m[id]);
    if (!s.includes(".FeaturesList") || !s.includes("suppresstooltip:!0")) {
      continue;
    }
    const refs = [...s.matchAll(/feature:([\w$]+)\.([\w$]+)/g)];
    if (!refs.length) return null;
    const alias = refs[0][1];
    const modMatch = new RegExp(
      `[,;\\s]${alias.replace(/\$/g, "\\$")}=t\\((\\d+)\\)`,
    ).exec(s);
    if (!modMatch) return null;
    const enumMod = req(modMatch[1]) as Record<string, unknown>;
    const ids = refs
      .filter((r) => r[1] === alias)
      .map((r) => enumMod[r[2]])
      .filter((v): v is number => typeof v === "number");
    return ids.length ? ids : null;
  }
  return null;
});

// ── Compat ────────────────────────────────────────────────

export interface CompatParts {
  deckLabel: (category: number) => string;
  steamOsLabel: (category: number) => string;
  deckIcon: (category: number) => AnyComponent;
  steamOsIcon: (category: number) => AnyComponent;
}

export const getCompatParts = once<CompatParts>(() => {
  const fns = functionsOfModule("#DeckVerified_CategoryLabel_Unknown");
  const deckLabel = fns.find((f) =>
    src(f).includes("#DeckVerified_CategoryLabel_Unknown"),
  );
  const steamOsLabel = fns.find((f) =>
    src(f).includes("#SteamOS_CategoryLabel_Unknown"),
  );
  // Steam's own Deck badge names the Deck icon getter it calls.
  const badge = fns.find((f) => src(f).includes("SteamDeckCompatInfo"));
  const deckIconName = /,\w+=([\w$]+)\(\w+\);return/.exec(src(badge))?.[1];
  const iconGetters = fns.filter(
    (f) =>
      f.length === 1 &&
      /^function [\w$]+\(\w+\)\{return \w+\[\w+\]\|\|/.test(src(f)),
  );
  const deckIcon = iconGetters.find((f) =>
    src(f).startsWith(`function ${deckIconName}(`),
  );
  const steamOsIcon = iconGetters.find((f) => f !== deckIcon);
  if (!deckLabel || !steamOsLabel || !deckIcon || !steamOsIcon) return null;
  return {
    deckLabel: deckLabel as CompatParts["deckLabel"],
    steamOsLabel: steamOsLabel as CompatParts["steamOsLabel"],
    deckIcon: deckIcon as CompatParts["deckIcon"],
    steamOsIcon: steamOsIcon as CompatParts["steamOsIcon"],
  };
});

// ── Collections ───────────────────────────────────────────

export interface CollectionsParts {
  listFor: (appId: number) => Array<{ id: string }>;
  Tile: AnyComponent;
  tileGrid: Record<string, number>;
  Grid: AnyComponent;
  Cell: AnyComponent;
  useScrollContainer: () => { Element?: unknown } | null;
}

export const getCollectionsParts = once<CollectionsParts>(() => {
  const store = (
    window as unknown as {
      collectionStore?: {
        GetCollectionListForAppID?: (id: number) => Array<{ id: string }>;
      };
    }
  ).collectionStore;
  if (typeof store?.GetCollectionListForAppID !== "function") return null;
  const req = getWebpackRequire();
  if (!req) return null;
  let Tile: AnyComponent | null = null;
  let tileGrid: Record<string, number> | null = null;
  let Grid: AnyComponent | null = null;
  let Cell: AnyComponent | null = null;
  let useScrollContainer: CollectionsParts["useScrollContainer"] | null = null;
  for (const id of Object.keys(req.m)) {
    const s = src(req.m[id]);
    const wanted =
      s.includes("bNoFooterFilter") ||
      s.includes("refItemsPerRow") ||
      s.includes('role:"gridcell"') ||
      s.includes("m_mountedSmartScrollContainers");
    if (!wanted) continue;
    let exports: Record<string, unknown>;
    try {
      exports = req(id) as Record<string, unknown>;
    } catch {
      continue;
    }
    for (const key of Object.keys(exports)) {
      let v: unknown;
      try {
        v = exports[key];
      } catch {
        continue;
      }
      const vs = isMemo(v) ? src(v.type) : src(v);
      if (!Tile && isMemo(v) && s.includes("bNoFooterFilter")) {
        const sizes = Object.values(exports).find(
          (o) =>
            !!o &&
            typeof o === "object" &&
            "childWidth" in (o as object) &&
            "childHeight" in (o as object),
        ) as Record<string, number> | undefined;
        if (sizes) {
          Tile = v as unknown as AnyComponent;
          tileGrid = sizes;
        }
      }
      if (
        !Grid &&
        isFn(v) &&
        vs.includes("refItemsPerRow") &&
        vs.includes("scaleGridItems")
      ) {
        Grid = v as AnyComponent;
      }
      if (
        !Cell &&
        isFn(v) &&
        vs.includes('role:"gridcell"') &&
        vs.length < 200
      ) {
        Cell = v as AnyComponent;
      }
      if (
        !useScrollContainer &&
        isFn(v) &&
        vs.includes("m_mountedSmartScrollContainers") &&
        vs.length < 300
      ) {
        useScrollContainer = v as CollectionsParts["useScrollContainer"];
      }
    }
  }
  if (!Tile || !tileGrid || !Grid || !Cell || !useScrollContainer) return null;
  return {
    listFor: (appId) => store.GetCollectionListForAppID!(appId) ?? [],
    Tile,
    tileGrid,
    Grid,
    Cell,
    useScrollContainer,
  };
});

// ── Localization ──────────────────────────────────────────

/**
 * Steam's own string for `token`, with `%1$s`-style slots filled from
 * `args`; `fallback` when Steam has no such token. Used on Steam's own
 * elements so they read exactly as on a Steam game, in Steam's language.
 */
export function steamText(
  token: string,
  fallback: string,
  ...args: Array<string | number>
): string {
  let template: string | undefined;
  try {
    const lm = (
      window as unknown as {
        LocalizationManager?: { LocalizeString?: (t: string) => string };
      }
    ).LocalizationManager;
    template = lm?.LocalizeString?.(token);
  } catch {
    template = undefined;
  }
  if (!template || template === token) return fallback;
  return template.replace(/%(\d+)\$s/g, (_m, n: string) => {
    const v = args[Number(n) - 1];
    return v == null ? "" : String(v);
  });
}

// ── Steam store tags ──────────────────────────────────────

interface StoreItem {
  GetTagIDs?: () => number[];
}
interface StoreItemCache {
  QueueAppRequest: (
    appId: number,
    request: Record<string, unknown>,
  ) => Promise<unknown>;
  GetApp: (appId: number) => StoreItem | undefined;
}

/** Steam's store-item cache (fetches any app's store data on demand). */
export const getStoreItemCache = once<StoreItemCache>(() => {
  const Cache = findExportInModule(
    ["QueueAppRequest", "QueueStoreItemRequest"],
    (v): v is { Get: () => StoreItemCache } =>
      isFn(v) && isFn((v as { Get?: unknown }).Get),
  );
  const cache = Cache?.Get();
  return cache && isFn(cache.QueueAppRequest) && isFn(cache.GetApp)
    ? cache
    : null;
});

/** Steam tags that name a way to play or a feature, not a genre: the
 *  features list already shows these (ids read live 2026-11-02). */
const NON_GENRE_TAGS: ReadonlySet<number> = new Set([
  4182, // Singleplayer
  3859, // Multiplayer
  1685, // Co-op
  3841, // Local Co-Op
  3843, // Online Co-Op
  4508, // Co-op Campaign
  7368, // Local Multiplayer
  1775, // PvP
  3878, // Competitive
  10816, // Split Screen
  7481, // Controller
  1756, // Great Soundtrack
]);

/**
 * The Steam store's own top tags for `steamAppId`, most-voted first, named
 * in the user's Steam language. Player-mode and feature tags are skipped.
 * Empty when Steam's cache or tag names are unavailable.
 */
export async function fetchSteamTopTags(
  steamAppId: number,
  count: number,
): Promise<string[]> {
  const cache = getStoreItemCache();
  const store = (
    window as unknown as {
      appStore?: {
        GetLocalizationForStoreTag?: (id: number) => string | undefined;
      };
    }
  ).appStore;
  if (!cache || !store?.GetLocalizationForStoreTag || steamAppId <= 0)
    return [];
  try {
    await cache.QueueAppRequest(steamAppId, { include_tag_count: 20 });
    const ids = cache.GetApp(steamAppId)?.GetTagIDs?.() ?? [];
    const names: string[] = [];
    for (const id of ids) {
      if (NON_GENRE_TAGS.has(id)) continue;
      const name = store.GetLocalizationForStoreTag(id);
      if (name) names.push(name);
      if (names.length >= count) break;
    }
    return names;
  } catch (e) {
    console.warn("[Unifideck NativeDetails] Steam tags unavailable:", e);
    return [];
  }
}
