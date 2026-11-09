/**
 * Which app ids get the native App-Details treatment.
 *
 * The same rule `AppDetailsPatch` has always used: a non-Steam shortcut
 * (app id above 2,000,000,000) that Unifideck manages. A shortcut the user
 * made themselves must keep stock Steam UI, so the decision defers to
 * `shouldPatchShortcut`, which reads our cache and, until that loads, the
 * shortcut's own Exe.
 */
import { isUnifideckCacheLoaded, isUnifideckGame } from "../../library-filters";
import { shouldPatchShortcut } from "../shortcut-ownership";

const SHORTCUT_APPID_FLOOR = 2_000_000_000;

export function isUnifideckAppId(appId: unknown): appId is number {
  if (typeof appId !== "number" || appId <= SHORTCUT_APPID_FLOOR) return false;
  try {
    return shouldPatchShortcut(
      appId,
      isUnifideckCacheLoaded(),
      isUnifideckGame(appId),
    );
  } catch {
    return false;
  }
}
