/**
 * Push the user's full owned Steam library ({title, appid} pairs) to
 * the backend.
 *
 * Kept separate from `collection-manager.ts` (which must stay free of
 * the `@decky/api` runtime so its unit tests can import it under vitest)
 * — this module is the thin RPC edge that the pure
 * `collectSteamOwnedGames` collector feeds.
 *
 * Two backend consumers read this: the Ubisoft Steam-linked filter
 * (title only — hides Ubisoft games the user owns on Steam but hasn't
 * installed, which never appear in `appmanifest`), see
 * `py_modules/unifideck/stores/ubisoft/library/steam_filter.py`; and
 * cross-store duplicate grouping's Steam cross-reference (needs the
 * appid too), see `py_modules/unifideck/core/game_grouping.py`.
 */
import { call } from "@decky/api";
import { rpcRoutes } from "../../api/rpc-routes";
import { collectSteamOwnedGames } from "./collection-manager";

/**
 * Best effort — on failure the backend filters just degrade to the
 * installed-games-only scan.
 */
export async function uploadSteamOwnedTitles(): Promise<void> {
  try {
    const games = collectSteamOwnedGames();
    if (games.length === 0) return;
    await call(rpcRoutes.updateSteamOwnedTitles, games);
  } catch (e) {
    console.warn("[Unifideck] uploadSteamOwnedTitles failed", e);
  }
}
