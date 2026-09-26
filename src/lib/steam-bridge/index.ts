/**
 * SteamBridge — barrel export.
 *
 * The single entry point for code that needs to interact
 * with Steam's CEF-injected globals (App Details, Router,
 * appStore lookups). Components import from this barrel and
 * never reach into `react-tree.ts` / `app-details-classes.ts`
 * directly.
 *
 * Rule of thumb : if a Steam internal name appears in a
 * component file, the component is doing it wrong. Push the
 * call into a SteamBridge method, return a typed result.
 */
export { SteamBridge } from "./SteamBridge";
export type {
  AppDetailsClassNames,
  ReactTreeMatcher,
  RouterPatchHandle,
} from "./SteamBridge";
export { findInReactTree } from "./react-tree";
export { getAppDetailsClasses } from "./app-details-classes";
export { getThemeableClasses } from "./app-details-classes";
export { addRouterPatch } from "./router-patch";
export type {
  ShortcutLaunchContext,
  ShortcutLaunchResult,
} from "./shortcut-types";
export { getShortcutRunGameId, isShortcutAppRunning } from "./shortcut-types";
export {
  createTemporaryShortcut,
  scheduleTemporaryShortcutCleanup,
} from "./temp-shortcut";
export { navigateToApp } from "./navigate-to-app";
