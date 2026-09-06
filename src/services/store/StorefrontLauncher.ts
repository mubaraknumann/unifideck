/**
 * StorefrontLauncher — open a store's shop with the session the user
 * already has, and refresh the library once they close it.
 *
 * Deliberately NOT part of `AuthDispatcher`. Opening a shop is not a
 * sign-in: it must not take the auth mutex, must not raise the
 * "Signing in…" toast, and must not resolve on `STORE_AUTH_*`. Sharing
 * that machinery would mean a shop that failed to open could flip the
 * store's row to `error`, where the settings UI renders no button at
 * all — stranding the user with no way to sign in or out over a
 * shopping trip.
 *
 * Two shapes, because the six stores authenticate two ways:
 *
 *   Epic / GOG / Amazon / Microsoft — sign in through the bundled Edge
 *     against a persistent profile. The shop opens in that same
 *     profile, so the live web session carries over.
 *
 *   Ubisoft / Battle.net — sign in inside a Wine prefix, in the vendor
 *     client. They have no browser session at all; their signed-in shop
 *     is the client's own Store/Shop tab, reached by opening the client
 *     in the auth prefix.
 *
 * **Nothing here touches auth state.** No cookie clearing, no
 * `store_auth`, no re-exchange. An earlier version re-ran each store's
 * OAuth exchange after the shop closed, so the stored tokens would
 * follow an account switch made in there. It had to go: every armed
 * flow polls the SAME shared CDP port, so three of them running at once
 * all captured the same Microsoft authorization code — GOG posted it to
 * `auth.gog.com` (HTTP 400) and legendary "registered" Epic with it.
 * Opening a shop must not be able to damage a sign-in. An account
 * switched inside the shop is now handled the ordinary way: the
 * post-close status check notices a dead token, and the user signs in
 * again.
 */
import { call } from "@decky/api";
import { EventBusClient } from "../../api/event-bus-client";
import { rpcRoutes } from "../../api/rpc-routes";
import { prepareForSync } from "../../lib/steam-bridge/prepare-sync";
import { watchAppStopped } from "../../lib/steam-bridge/shortcut-types";
import { authStore } from "../../stores/auth-store";
import {
  launchAmazonStorefrontViaShortcut,
  launchEpicStorefrontViaShortcut,
  launchGogStorefrontViaShortcut,
  launchMicrosoftStorefrontViaShortcut,
} from "../../utils/authShortcutLaunch";
import { launchWrapperAuthViaShortcut } from "../../lib/steam-bridge/wrapper-shortcut-launch";
import { BATTLENET_SHORTCUT_CONFIG } from "../../utils/battlenetShortcutLaunch";
import { UBISOFT_SHORTCUT_CONFIG } from "../../utils/ubisoftShortcutLaunch";
import { storeReportsConnected } from "../auth/store-status";
import type { ShortcutLaunchResult } from "../../lib/steam-bridge/shortcut-types";
import type { StoreId } from "../../types/api";

/** Stores whose shop is a web page in the shared Edge profile. */
const BROWSER_STOREFRONTS: Partial<
  Record<StoreId, () => Promise<ShortcutLaunchResult>>
> = {
  epic: launchEpicStorefrontViaShortcut,
  gog: launchGogStorefrontViaShortcut,
  amazon: launchAmazonStorefrontViaShortcut,
  microsoft: launchMicrosoftStorefrontViaShortcut,
};

/**
 * Stores whose shop is a tab inside their own Windows client.
 *
 * Note what is NOT here: the `store_auth(store, "start")` kick a real
 * sign-in makes. That call arms the wrapper session monitor and emits
 * `STORE_AUTH_COMPLETE` when it fires, which on the shop path only
 * produced a redundant library sync per press — and coincided with a
 * Ubisoft session going invalid. Opening the client to browse must not
 * pretend to be a sign-in.
 */
const CLIENT_STOREFRONTS: Partial<
  Record<StoreId, () => Promise<ShortcutLaunchResult>>
> = {
  ubisoft: () =>
    launchWrapperAuthViaShortcut(UBISOFT_SHORTCUT_CONFIG, "storefront"),
  battlenet: () =>
    launchWrapperAuthViaShortcut(BATTLENET_SHORTCUT_CONFIG, "storefront"),
};

/**
 * Let the backend plant a browser session for `store`, if it can.
 *
 * Only Amazon has anything to do here: nile signs in through Amazon's
 * device-registration flow, which authorises the device but leaves the
 * Edge profile without the cookies a signed-in amazon.com needs, so the
 * shop opened logged out. The backend exchanges nile's refresh token
 * for website cookies and writes them into the profile — which must
 * happen BEFORE Edge starts, since Edge owns that file while running.
 *
 * Awaited but never trusted: a failure just means the shop opens with
 * whatever session was already there.
 */
async function prepareWebSession(store: StoreId): Promise<void> {
  try {
    await call<[StoreId], unknown>(rpcRoutes.prepareStoreWebSession, store);
  } catch (e) {
    console.warn(`[StorefrontLauncher:${store}] web session prep failed:`, e);
  }
}

/** Whether this store has a shop the cart can open at all. */
export function hasStorefront(store: StoreId): boolean {
  return store in BROWSER_STOREFRONTS || store in CLIENT_STOREFRONTS;
}

/**
 * Refresh auth + library once the shop window closes.
 *
 * Order matters. Refresh auth status FIRST, so a token that died in
 * there (the user signed out, or switched accounts) flips the row back
 * to "Sign in". Only then sync — and only if the store still reports
 * connected, because `request_auth_sync` triggers the post-sync
 * reconcile sweep, which REMOVES a logged-out store's shortcuts.
 * Syncing a store we just lost would delete the user's library tiles
 * for it.
 */
async function settleAfterClose(store: StoreId): Promise<void> {
  await authStore.refetch();
  if (!(await storeReportsConnected(store))) {
    console.log(
      `[StorefrontLauncher:${store}] not connected after shop — skipping sync`,
    );
    return;
  }
  try {
    await prepareForSync();
    await call<[StoreId], unknown>(rpcRoutes.requestAuthSync, store);
  } catch (e) {
    console.error(`[StorefrontLauncher:${store}] post-shop sync failed:`, e);
  }
}

/**
 * Open `store`'s shop, then refresh state once it closes.
 *
 * Resolves as soon as the window has been asked to open — the refresh
 * continues in the background, keyed off Steam's app-stopped
 * notification, so the QAM stays responsive while the user shops.
 */
export async function openStorefront(
  store: StoreId,
): Promise<ShortcutLaunchResult> {
  const launch = BROWSER_STOREFRONTS[store] ?? CLIENT_STOREFRONTS[store];
  if (!launch) {
    return { success: false, error: `No storefront for ${store}` };
  }
  EventBusClient.bumpToFast();
  if (store in BROWSER_STOREFRONTS) await prepareWebSession(store);
  const result = await launch();
  const appId = result.app_id;
  if (result.success && appId) {
    watchAppStopped(appId, () => {
      void settleAfterClose(store);
    });
  }
  return result;
}
