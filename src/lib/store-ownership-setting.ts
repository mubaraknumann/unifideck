/**
 * Store-ownership ribbon setting: one boolean, per device.
 *
 * Same storage model as the collections toggle (`collection-manager.ts`):
 * localStorage plus a broadcast event, so the QAM toggle and the running
 * ribbon module agree without a restart. Default ON, because the ribbon
 * only appears on games the user already owns elsewhere and costs nothing on
 * every other store page.
 */
export const STORE_OWNERSHIP_ENABLED_KEY = "unifideck:storeOwnership.enabled";
export const STORE_OWNERSHIP_ENABLED_EVENT =
  "unifideck:store-ownership-enabled-change";

export function isStoreOwnershipEnabled(): boolean {
  try {
    return window.localStorage.getItem(STORE_OWNERSHIP_ENABLED_KEY) !== "0";
  } catch {
    return true;
  }
}

export function setStoreOwnershipEnabled(on: boolean): void {
  try {
    window.localStorage.setItem(STORE_OWNERSHIP_ENABLED_KEY, on ? "1" : "0");
  } catch {
    /* localStorage unavailable: the toggle still applies for this session */
  }
  try {
    window.dispatchEvent(
      new CustomEvent(STORE_OWNERSHIP_ENABLED_EVENT, { detail: on }),
    );
  } catch {
    /* CEF can deny CustomEvent in rare half-loaded states */
  }
}
