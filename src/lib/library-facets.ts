/**
 * Library Facets — per-shortcut enrichment data for Steam's native
 * library Sort menu + Library Filters, plus shortcut-keyed
 * per-device compatibility resolution.
 *
 * Our custom tabs render through Steam's own grid, which sorts and
 * filters purely on `AppOverview` field values. Non-Steam shortcuts
 * have those fields empty, so this module pulls a per-shortcut
 * `FacetRecord` from the backend (`get_overview_enrichment`, a pure
 * reshape of the metadata/compat caches) and the overview-enrichment
 * layer writes them onto the live overviews.
 *
 * The same records carry `protondb_tier` / `compat_status` keyed by the
 * **shortcut** AppID, so the compat tab no longer depends on fuzzy
 * title matching against the compat cache.
 *
 * Backend keys every record under BOTH the signed and unsigned 32-bit
 * forms of the shortcut AppID; we store whatever it sends, so a lookup
 * by either form hits.
 */
import { call } from "@decky/api";
import { rpcRoutes } from "../api/rpc-routes";
import { unwrapRpcEnvelope } from "../api/useRPC";
import type { CompatTrack } from "./steam-bridge/compat-packed";
import { getCachedCompatByTitle } from "./protondb-cache";
import type {
  CompatStatus,
  GameCompatInfo,
  ProtonDBTier,
} from "./protondb-cache";

/** One shortcut's facet data — mirrors the backend `FacetRecord`
 *  (`rpc/mixins/_library_facets.py`). All fields are best-effort;
 *  any may be null/empty when the source cache is cold. */
export interface FacetRecord {
  steam_app_id: number;
  /** Sort dimensions */
  metacritic: number | null;
  /** Raw Steam release-date string (e.g. "21 Jun, 2022"); converted
   *  to the unix value Steam sorts on at apply time. */
  release_date: string;
  recommendations_total: number | null;
  review_score: number | null;
  review_percentage: number | null;
  /** First-seen timestamp from the backend (`_stamp_date_added`).
   *  Deliberately NOT projected onto `AppOverview.rt_purchased_time`
   *  — Steam's Home "Recent Games" shelf ranks on that field, so doing
   *  so pushed every freshly-synced game to the top of the shelf. Kept
   *  here for a future self-managed Date-Added sort; do not restore
   *  the overview write (see `steam-bridge/overview-enrichment.ts`). */
  date_added_unix: number;
  /** Filter dimensions */
  /** Rating for the device this is running on, already resolved by the
   *  backend (Valve's category, else our ProtonDB-optimism bump).
   *  0 Unknown · 1 Unsupported · 2 Playable · 3 Verified. */
  compat_category: number;
  compat_status: string;
  /** Every device's raw category, for the packed bitfield Steam's own
   *  filters read. Not interchangeable with `compat_category`: the
   *  SteamOS track uses a different 3-value enum. */
  compat_categories?: Partial<Record<CompatTrack, number>>;
  store_category: number[];
  store_tag: number[];
  /** Shortcut-keyed compat — no title matching */
  protondb_tier: string | null;
}

const facetByAppId = new Map<number, FacetRecord>();
let loaded = false;

/** Synchronous facet lookup by AppID (signed or unsigned). */
export function getFacet(appId: number): FacetRecord | null {
  return facetByAppId.get(appId) ?? null;
}

export function isFacetsLoaded(): boolean {
  return loaded;
}

/** Test-only: reset the in-memory cache so cases don't bleed state
 *  (the maps + `loaded` flag are module-level singletons). */
export function __resetFacetsForTest(): void {
  facetByAppId.clear();
  loaded = false;
}

/** All AppIDs we hold a facet for — used by the enrichment pass to
 *  iterate exactly the shortcuts that have data. */
export function getFacetAppIds(): number[] {
  return [...facetByAppId.keys()];
}

/**
 * Derive a `GameCompatInfo` from the shortcut-keyed facet — no title
 * matching. Returns null when this AppID has no facet (caller falls
 * back to the title-keyed compat cache).
 *
 * `compat_status` is already resolved for the running device, so this
 * needs no device branch of its own.
 */
export function getCompatByShortcutAppId(appId: number): GameCompatInfo | null {
  const f = facetByAppId.get(appId);
  if (!f) return null;
  return {
    tier: (f.protondb_tier as ProtonDBTier | null) ?? null,
    status: (f.compat_status as CompatStatus) || "unknown",
    steamAppId: f.steam_app_id || null,
  };
}

/**
 * Load the per-shortcut enrichment map from the backend. Idempotent
 * unless `force` is passed (used to refresh after a library sync).
 * Catches every error so a backend outage degrades to "no enrichment"
 * rather than breaking plugin boot.
 */
export async function loadFacets(force = false): Promise<void> {
  if (loaded && !force) return;
  try {
    const raw = await call<[], unknown>(rpcRoutes.getOverviewEnrichment);
    const map = unwrapRpcEnvelope<
      Record<string, FacetRecord> | null | undefined
    >(raw, { route: rpcRoutes.getOverviewEnrichment });
    const entries = Object.entries(map ?? {});
    // A forced reload during a library sync can race the backend: the
    // metadata/compat caches are briefly mid-rebuild and the RPC
    // returns empty. Don't wipe good enrichment in that window — keep
    // what we have and let the next sync-completed refresh fill it.
    if (entries.length === 0 && facetByAppId.size > 0) {
      return;
    }
    facetByAppId.clear();
    for (const [key, rec] of entries) {
      const id = Number(key);
      if (!Number.isNaN(id) && rec) facetByAppId.set(id, rec);
    }
    loaded = true;
    console.log(
      `[Unifideck] Loaded overview enrichment for ${facetByAppId.size} shortcut-id keys`,
    );
  } catch (e) {
    console.error("[Unifideck] loadFacets failed", e);
  }
}

/**
 * Compat for a Unifideck shortcut: facet first, title second.
 *
 * The two-step fallback is shared by the library-tab filter and the
 * plugin's own library view, which each used to carry their own copy.
 * Facet lookup is authoritative — the backend resolved this shortcut to
 * a real Steam AppID through the central title matcher, so no fuzzy
 * match against the lossy `display_name` is needed. The title path only
 * covers shortcuts the metadata phase has not mapped yet.
 */
export function resolveCompatForShortcut(
  appId: number | null | undefined,
  title: string | null | undefined,
): GameCompatInfo | null {
  if (appId != null) {
    const byFacet = getCompatByShortcutAppId(appId);
    if (byFacet) return byFacet;
  }
  return title ? getCachedCompatByTitle(title) : null;
}
