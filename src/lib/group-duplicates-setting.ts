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

export function isGroupDuplicatesEnabled(): boolean {
  try {
    return window.localStorage.getItem(GROUP_DUPLICATES_KEY) === "1";
  } catch {
    return false;
  }
}

export function setGroupDuplicatesEnabled(on: boolean): void {
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
