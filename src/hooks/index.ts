/**
 * Domain hooks subpackage — barrel export.
 *
 * Each hook in this package wraps one or more contexts
 * (Phase F2) and exposes a focused, business-oriented API
 * that components can call without juggling multiple
 * contexts manually. The 9 hooks cover the entire frontend's
 * interaction surface: auth, install/launch actions, game
 * info fetching, view-mode persistence, play-section state,
 * download progress, toasts, and Steam library access.
 *
 * Rule of thumb: components import from this barrel; they
 * NEVER reach into individual hook files. Renaming an
 * internal hook is therefore a one-file change in this
 * package.
 */
export { useStoreAuth } from "./useStoreAuth";
export { useGameActions } from "./useGameActions";
export { useGameInfo } from "./useGameInfo";
export { useViewMode } from "./useViewMode";
export { usePlaySection } from "./usePlaySection";
export { useDownloadProgress } from "./useDownloadProgress";
export { useToast } from "./useToast";
export { useSteamLibrary } from "./useSteamLibrary";
export { useStorageConfig } from "./useStorageConfig";
export { useInstallFlow } from "./useInstallFlow";
export { useSyncCooldown } from "./useSyncCooldown";
export { useXboxPcStatus, xboxPcSnapshot } from "./useXboxPcStatus";
