/**
 * "Group duplicates" setting — whether cross-store copies of the same
 * game collapse to a single tile (in "All Games", "Great on Deck", and
 * "Installed") or show as separate items, one per store.
 *
 * Default OFF: every store's shortcut shows up as its own tile unless
 * the user explicitly opts into grouping. Mirrors `collection-manager`'s
 * `isCollectionsEnabled`/`setCollectionsEnabled` pattern — localStorage
 * (this is purely a display filter, no backend involvement) plus a
 * broadcast event so module-level consumers outside React (the native
 * tab filters in `library-filters`, and `tab-container`'s live refresh)
 * stay in sync without a reload.
 *
 * Deliberately has NO imports from `library-filters` or `tab-container`
 * — both of those import FROM here, and either direction back would be
 * a cycle.
 */
const GROUP_DUPLICATES_KEY = "unifideck:group-duplicates.enabled";
export const GROUP_DUPLICATES_EVENT = "unifideck:group-duplicates-change";

/** The setting, read from storage once. The tab filters ask for it once per
 *  app on every filter pass (thousands of calls per library render), and
 *  {@link setGroupDuplicatesEnabled} is the only writer in this JS context,
 *  so the cached value cannot go stale. */
let cached: boolean | null = null;

export function isGroupDuplicatesEnabled(): boolean {
  if (cached === null) {
    try {
      cached = window.localStorage.getItem(GROUP_DUPLICATES_KEY) === "1";
    } catch {
      cached = false;
    }
  }
  return cached;
}

/** Test seam: forget the cached value so the next read goes to storage. */
export function resetGroupDuplicatesCache(): void {
  cached = null;
}

export function setGroupDuplicatesEnabled(on: boolean): void {
  cached = on;
  try {
    window.localStorage.setItem(GROUP_DUPLICATES_KEY, on ? "1" : "0");
  } catch {
    /* localStorage unavailable — worst case the setting doesn't persist */
  }
  try {
    window.dispatchEvent(
      new CustomEvent(GROUP_DUPLICATES_EVENT, { detail: on }),
    );
  } catch {
    /* CEF can deny CustomEvent in rare half-loaded states */
  }
}
