/**
 * Steam Store "already owned elsewhere" ribbon: navigation detection.
 *
 * The Gaming Mode store is a separate BrowserView drawn ABOVE Steam's own
 * window, so nothing rendered from this React tree can appear on a store
 * page. This module only notices where the store is; the backend
 * (`show_store_ownership`) decides whether the game is owned elsewhere and,
 * only then, draws the ribbon into the page over CDP
 * (`py_modules/unifideck/cdp/store_ribbon.py`).
 *
 * Detection uses Steam's own store-browser callback lists,
 * `GamepadUIMainWindowInstance.m_StoreBrowser.StartLoadingCallbacks` and
 * `.FinishedRequestCallbacks`, with no CDP. Verified on-device (2026-10-02):
 * both fire with the URL first for steam://openurl navigations, in-page link
 * clicks and Back. On a page never visited, finished came up to 6 s after
 * loading started, while the page's blocks existed after ~2 s. The browser
 * object is created lazily and Steam rebuilds it (and its window) after a UI
 * restart, so a once-a-second check re-attaches whenever either changes.
 *
 * Back/forward loads a fresh document, so every finished request is sent
 * and none is deduplicated by URL.
 */
import { call } from "@decky/api";
import { Router } from "@decky/ui";
import i18n from "i18next";
import { rpcRoutes } from "../../api/rpc-routes";
import { unwrapRpcEnvelope } from "../../api/useRPC";
import { STORE_VISUALS } from "../../types/store";
import { storeIconSpecs, type IconSpec } from "./store-icon-spec";
import type {
  GamepadMainWindowInternals,
  SteamStoreBrowser,
} from "../../types/steam";
import {
  STORE_OWNERSHIP_ENABLED_EVENT,
  isStoreOwnershipEnabled,
} from "../store-ownership-setting";

/** A Steam Store app page; the AppID must end at a path/query/hash boundary. */
export const STORE_APP_URL_RE =
  /^https:\/\/store\.steampowered\.com\/app\/(\d+)(?:[/?#]|$)/;

const STORE_ROUTE = "/steamweb";
/** Coalesces redirect bursts (app → agecheck → app) into one draw. */
const DEBOUNCE_MS = 300;
/** The store BrowserView is created lazily after the route changes. */
const RESOLVE_RETRIES = 8;
const RESOLVE_INTERVAL_MS = 250;
/** How often an active ribbon re-checks that it is attached (see
 *  `ensureAttached`). */
const ATTACH_CHECK_MS = 1000;
/** Warn once when no Gaming Mode window appears for this long (Desktop Mode). */
const NO_WINDOW_WARN_MS = 60 * 1000;
const LOG = "[Unifideck] Store ownership ribbon:";

/** Translated ribbon text. The page has no i18next, so it travels with the call. */
export interface RibbonStrings {
  tag_owned: string;
  tag_cloud: string;
  message_owned: string;
  message_cloud: string;
  /** The neutral streaming line: it streams, but not via Game Pass or a known purchase. */
  message_xcloud: string;
  installed: string;
  via: string;
  dir: "ltr" | "rtl";
  store_labels: Record<string, string>;
  /** Store logos as SVG data; the page falls back to a dot without one. */
  store_icons: Record<string, IconSpec>;
  /** Notes on an owned Xbox chip: where it plays, and "Gold" (needs a
   *  subscription). Keys match the backend's `OwnedCopy.platform`. */
  note_labels: Record<string, string>;
}

interface ShowStoreOwnershipResult {
  shown?: boolean;
  reason?: string;
  stores?: string[];
}

export function parseStoreAppId(url: unknown): number | null {
  if (typeof url !== "string") return null;
  const match = STORE_APP_URL_RE.exec(url);
  if (!match) return null;
  const appId = Number(match[1]);
  return Number.isSafeInteger(appId) && appId > 0 ? appId : null;
}

export function buildRibbonStrings(): RibbonStrings {
  const t = (key: string): string => String(i18n.t(key));
  const storeLabels: Record<string, string> = {};
  for (const [id, visual] of Object.entries(STORE_VISUALS)) {
    storeLabels[id] = visual.display_name;
  }
  return {
    tag_owned: t("storeOwnership.tagOwned"),
    // The streaming lines: Game Pass (owned or not), or plain Xbox Cloud
    // Gaming when the reason it streams is unknown. Streaming an owned game
    // is the "Cloud" note on the owned chip instead.
    tag_cloud: t("storeOwnership.tagStreamable"),
    message_owned: t("storeOwnership.messageOwned"),
    message_cloud: t("storeOwnership.messageGamePass"),
    message_xcloud: t("storeOwnership.messageXboxCloud"),
    installed: t("storeOwnership.installed"),
    via: t("storeOwnership.via"),
    dir: typeof i18n.dir === "function" && i18n.dir() === "rtl" ? "rtl" : "ltr",
    store_labels: storeLabels,
    store_icons: storeIconSpecs(),
    note_labels: {
      pc: t("storeOwnership.platformPc"),
      console: t("storeOwnership.platformConsole"),
      pc_console: t("storeOwnership.platformPcConsole"),
      play_anywhere: t("storeOwnership.platformPlayAnywhere"),
      gold: t("storeOwnership.noteGold"),
      cloud: t("storeOwnership.noteCloud"),
    },
  };
}

/** react-router history hands listeners a location (v4) or `{ location }` (v5). */
function pathnameOf(update: unknown): string {
  const u = update as
    | { pathname?: string; location?: { pathname?: string } }
    | undefined;
  return u?.location?.pathname ?? u?.pathname ?? "";
}

function gamepadWindow(): GamepadMainWindowInternals | undefined {
  return Router.WindowStore?.GamepadUIMainWindowInstance as
    | GamepadMainWindowInternals
    | undefined;
}

/**
 * Start watching the Gaming Mode store. Returns the disposer for
 * `teardown.ts`. Honours the QAM toggle live: off unregisters everything.
 */
export function startStoreOwnershipRibbon(): () => void {
  const warned = new Set<string>();
  const warnOnce = (key: string, message: string) => {
    if (warned.has(key)) return;
    warned.add(key);
    console.warn(`${LOG} ${message}`);
  };

  let active = false;
  let startedAt = 0;
  let win: GamepadMainWindowInternals | undefined;
  let browser: SteamStoreBrowser | null = null;
  let registrations: Array<{ Unregister(): void }> = [];
  let unlisten: (() => void) | null = null;
  let debounce: ReturnType<typeof setTimeout> | undefined;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let watchdog: ReturnType<typeof setInterval> | undefined;

  const draw = async (appId: number) => {
    try {
      const raw = await call<[number, RibbonStrings], unknown>(
        rpcRoutes.showStoreOwnership,
        appId,
        buildRibbonStrings(),
      );
      const result = unwrapRpcEnvelope<ShowStoreOwnershipResult | null>(raw, {
        route: rpcRoutes.showStoreOwnership,
        throwing: false,
      });
      if (result?.reason === "cdp_unavailable") {
        warnOnce(
          "cdp",
          "Steam's CEF debugger is unreachable (backend `cdp.port`), so the ribbon cannot be drawn on Store pages. See the Unifideck plugin log",
        );
      }
    } catch (e) {
      warnOnce("rpc", `show_store_ownership failed: ${String(e)}`);
    }
  };

  const onNavigate = (url: unknown) => {
    if (!active) return;
    clearTimeout(debounce);
    const appId = parseStoreAppId(url);
    if (appId === null) return;
    debounce = setTimeout(() => void draw(appId), DEBOUNCE_MS);
  };

  const unregister = () => {
    for (const registration of registrations) registration.Unregister();
    registrations = [];
    browser = null;
  };

  /** True once there is nothing left to retry (registered, or unusable). */
  const ensureRegistered = (): boolean => {
    const next = win?.m_StoreBrowser;
    if (!next) return false;
    if (next === browser) return true;
    unregister();
    browser = next;
    const finished = next.FinishedRequestCallbacks;
    if (typeof finished?.Register !== "function") {
      warnOnce(
        "register",
        "GamepadUIMainWindowInstance.m_StoreBrowser.FinishedRequestCallbacks has no Register(). Steam changed its store browser, so the ribbon is disabled",
      );
      return true;
    }
    registrations.push(finished.Register((url) => onNavigate(url)));
    // The earlier signal: drawing as the page starts loading lets the
    // backend's work overlap the page's own render; the page script waits
    // for its anchors, and the backend retries while the target still holds
    // the previous page. Finished stays as the backstop.
    const starting = next.StartLoadingCallbacks;
    if (typeof starting?.Register === "function") {
      registrations.push(starting.Register((url) => onNavigate(url)));
    }
    onNavigate(next.m_URL); // a store page already open before we registered
    return true;
  };

  const resolveSoon = (attempt = 0) => {
    clearTimeout(retry);
    if (!active || ensureRegistered()) return;
    if (attempt + 1 >= RESOLVE_RETRIES) return; // the watchdog keeps trying
    retry = setTimeout(() => resolveSoon(attempt + 1), RESOLVE_INTERVAL_MS);
  };

  const onRoute = (update: unknown) => {
    if (pathnameOf(update).startsWith(STORE_ROUTE)) resolveSoon();
  };

  /** Follow Steam's current Gaming Mode window: listen to its history and
   *  register on its store browser. Runs on activation and then every
   *  {@link ATTACH_CHECK_MS}, because Steam rebuilds the window and the
   *  store browser after a UI restart and a reference taken once goes stale
   *  without any error (measured 2026-10-02: the ribbon was off for every
   *  store after the post-sync restart). Costs two property reads. */
  const ensureAttached = () => {
    if (!active) return;
    const current = gamepadWindow();
    if (!current) {
      if (Date.now() - startedAt >= NO_WINDOW_WARN_MS) {
        warnOnce(
          "window",
          "Router.WindowStore.GamepadUIMainWindowInstance not found (desktop / non-Big-Picture UI). The ribbon only works in Gaming Mode",
        );
      }
      return;
    }
    if (current !== win) {
      unlisten?.();
      unlisten = null;
      unregister();
      win = current;
      const history = current.m_history;
      if (typeof history?.listen === "function") {
        unlisten = history.listen(onRoute) ?? null;
      } else {
        warnOnce(
          "history",
          "GamepadUIMainWindowInstance.m_history.listen not found. The ribbon attaches to the Store only on its once-a-second check",
        );
      }
    }
    ensureRegistered();
  };

  const activate = () => {
    if (active) return;
    active = true;
    startedAt = Date.now();
    ensureAttached();
    watchdog = setInterval(ensureAttached, ATTACH_CHECK_MS);
  };

  const deactivate = () => {
    active = false;
    clearTimeout(debounce);
    clearTimeout(retry);
    clearInterval(watchdog);
    unregister();
    unlisten?.();
    unlisten = null;
    win = undefined;
  };

  const onSetting = (e: Event) => {
    const on = (e as CustomEvent<boolean>).detail;
    if (on === true) activate();
    else if (on === false) deactivate();
  };

  window.addEventListener(STORE_OWNERSHIP_ENABLED_EVENT, onSetting);
  if (isStoreOwnershipEnabled()) activate();

  return () => {
    window.removeEventListener(STORE_OWNERSHIP_ENABLED_EVENT, onSetting);
    deactivate();
  };
}
