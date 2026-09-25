/**
 * SteamBridge — façade over Steam internals.
 *
 * Wraps every Steam API call the plugin needs : app overview
 * lookups, router patches, lifetime notifications, controller
 * config queries. Each method has a typed signature and a
 * defensive fallback so the plugin degrades gracefully if a
 * Steam update breaks an internal.
 *
 * Singleton instance; instantiated by the plugin entry and
 * passed via React context to consumers.
 */
import { findInReactTree, type ReactTreeMatcher } from "./react-tree";
import {
  getAppDetailsClasses,
  type AppDetailsClassNames,
} from "./app-details-classes";
import { addRouterPatch, type RouterPatchHandle } from "./router-patch";
import { getShortcutRunGameId } from "./shortcut-types";
import type {
  SteamApp,
  SteamAppOverview,
  SteamCollection,
  Unregisterable,
} from "../../types/steam";
export type { AppDetailsClassNames, ReactTreeMatcher, RouterPatchHandle };

type AppLifetimeCallback = (notification: {
  unAppID: number;
  bRunning: boolean;
  nInstanceID: number;
}) => void;

type GameActionCallback = (
  gameActionId: number,
  appId: string,
  action: string,
  launchSource: number,
) => void;

/**
 * Single boundary between Unifideck and Steam's runtime
 * internals (window.SteamClient, window.appStore, the
 * Router, app-details React tree). Every other module
 * imports from here, never directly from Steam globals.
 *
 * The bridge :
 *  - centralises the spoofing/patching logic so a Steam
 *    update breaks one file, not the whole codebase ;
 *  - returns typed projections instead of raw any-shapes ;
 *  - logs and degrades gracefully when an internal symbol
 *    has been renamed or removed.
 */
export class SteamBridge {
  /** True once at least one SteamClient method has been seen.
   *  Used by the plugin to delay UI mount until Steam is ready. */
  isReady(): boolean {
    return typeof window.SteamClient?.Apps?.GetAppOverview === "function";
  }

  /** Look up an `AppOverview` for any Steam appid (real game
   *  or non-Steam shortcut). Returns null if Steam can't find
   *  the appid; never throws. */
  getAppOverview(appId: number): SteamAppOverview | null {
    try {
      return window.SteamClient?.Apps?.GetAppOverview(appId) ?? null;
    } catch {
      return null;
    }
  }

  /** Subscribe to "app started / app stopped" notifications.
   *  Returns an Unregisterable; callers must call
   *  `.unregister()` in their cleanup path. */
  onAppLifetime(callback: AppLifetimeCallback): Unregisterable {
    const apis = window.SteamClient?.GameSessions;
    if (!apis?.RegisterForAppLifetimeNotifications) {
      return { unregister: () => {} };
    }

    return apis.RegisterForAppLifetimeNotifications(callback);
  }

  /** Subscribe to "Play" / "Install" / "Update" button clicks
   *  via Steam's GameAction event stream. */
  onGameAction(callback: GameActionCallback): Unregisterable {
    const apis = window.SteamClient?.Apps;
    if (!apis?.RegisterForGameActionStart) {
      return { unregister: () => {} };
    }

    return apis.RegisterForGameActionStart(callback);
  }

  /** Cancel an in-flight game action (Play / Install / etc).
   *  Used to intercept Play and re-route to our launcher. */
  cancelGameAction(gameActionId: number): void {
    window.SteamClient?.Apps?.CancelGameAction(gameActionId);
  }

  /** Get all owned + non-Steam apps. The two lists overlap
   *  for shortcuts (a non-Steam game registered in
   *  shortcuts.vdf appears in both). De-dup is the caller's
   *  responsibility. */
  getAllApps(): SteamApp[] {
    const apis = window.SteamClient?.Apps;
    if (!apis) return [];
    try {
      const owned = apis.GetOwnedApps?.() ?? [];
      const nonSteam = apis.GetNonSteamApps?.() ?? [];
      return [...owned, ...nonSteam];
    } catch {
      return [];
    }
  }

  /** Read user's Steam collections. Used by the unified
   *  library view to filter games by collection. */
  getCollections(): SteamCollection[] {
    const map = window.collectionStore?.userCollections;
    return map ? Array.from(map.values()) : [];
  }

  /** Launch a non-Steam shortcut via Steam's RunGame API.
   *
   *  ``RunGame``'s first argument must be the 64-bit shortcut
   *  *gameID*, NOT the 32-bit appid — passing the raw appid is a
   *  silent no-op for non-Steam shortcuts (this is why clicking
   *  Play did nothing). We resolve it with ``getShortcutRunGameId``
   *  and pass empty launch options + ``(-1, 100)`` so Steam uses
   *  the shortcut's own stored ``LaunchOptions``. Mirrors the
   *  proven auth-flow / controllerConfig call shape. */
  runGame(appId: number): void {
    window.SteamClient?.Apps?.RunGame(getShortcutRunGameId(appId), "", -1, 100);
  }

  /** Force-terminate a running shortcut: the Play section's Stop (✕).
   *
   *  Same rule as ``runGame`` above: Steam's ``TerminateApp`` takes the
   *  64-bit shortcut *gameID* (Steam's own bundle passes ``GetGameID()``).
   *  Passing the 32-bit appid was a silent no-op, measured 2026-09-24: Steam
   *  logged ``TerminateGame`` and stopped nothing, so Stop never closed any
   *  Unifideck game. ``terminateShortcutApp`` in ``wrapper-shortcut-launch``
   *  already converted; this path did not. */
  terminateApp(appId: string, force: boolean = false): void {
    window.SteamClient?.Apps?.TerminateApp(
      getShortcutRunGameId(Number(appId)),
      force,
    );
  }

  /** Patch a router route to mount our React content at the
   *  given path. Returns a handle whose `.remove()` undoes
   *  the patch. */
  addRouterPatch(
    path: string,
    patch: (props: unknown) => unknown,
  ): RouterPatchHandle {
    return addRouterPatch(path, patch);
  }

  /** Find a node deep in a React tree matching the predicate.
   *  Used to inject our PlayButton wrapper into Steam's
   *  AppDetails component. */
  findInReactTree<T = unknown>(
    root: unknown,
    matcher: ReactTreeMatcher,
  ): T | null {
    return findInReactTree<T>(root, matcher);
  }

  /** Resolve the dynamic CSS class names Steam uses for app
   *  details (they change every Steam build). */
  getAppDetailsClasses(): AppDetailsClassNames {
    return getAppDetailsClasses();
  }
}
