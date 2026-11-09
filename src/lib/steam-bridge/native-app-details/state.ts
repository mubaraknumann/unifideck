/**
 * Whether the native App-Details integration is live this session.
 *
 * Set by `startNativeAppDetails` once Steam's action button and tabbed
 * page are both patched. `AppDetailsPatch` reads it: while it is true the
 * legacy custom play row and info panel are not injected and Steam's own
 * UI is not hidden. While it is false (a Steam update moved a seam) the
 * legacy UI takes over, so a game page is never left without our actions.
 */
let active = false;

export function isNativeAppDetailsActive(): boolean {
  return active;
}

export function setNativeAppDetailsActive(value: boolean): void {
  active = value;
}
