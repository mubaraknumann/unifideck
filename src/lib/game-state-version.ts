/**
 * Per-appId version counter for AppDetails injection keys.
 *
 * Bumping the version for an appId re-keys the injected
 * `<PlaySectionWrapper>` and `<GameInfoPanel>` so React fully
 * re-mounts them on game-state changes (install / uninstall).
 *
 * Lives in `lib/` rather than `views/AppDetailsPatch.tsx` so
 * hooks (`useGameActions`) can bump the version without
 * importing `views/`, which would create a cycle :
 *   useGameActions → views/AppDetailsPatch
 *     → components/play
 *       → PlaySectionWrapper → NotInstalledButtons → useGameActions.
 */
import { toUnsignedAppId } from "./steam-bridge/appid";

/** Keyed by the UNSIGNED appid. A shortcut appid reaches here in both forms:
 *  the backend's `app_id` is signed (`generate_app_id`), while the app-details
 *  route's `overview.appid` is unsigned. Bumping one form and reading the
 *  other silently never matched, so the detail page's store switcher kept a
 *  stale sibling list after a re-sync. Real Steam appids are unaffected. */
const versions = new Map<number, number>();

/** Read the current version for an appId (0 by default), in either form. */
export function getGameStateVersion(appId: number): number {
  return versions.get(toUnsignedAppId(appId)) ?? 0;
}

/** Increment the version so the next patch re-mounts the
 *  Unifideck overrides for this appId, in either form.
 *  Idempotent and cheap. */
export function bumpGameStateVersion(appId: number): void {
  const key = toUnsignedAppId(appId);
  versions.set(key, (versions.get(key) ?? 0) + 1);
}
