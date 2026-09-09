/**
 * plugin-update — shared primitives for the plugin self-update feature.
 *
 * Split out of `components/settings/PluginUpdater.tsx` when the updater's
 * four stacked QAM controls were folded behind a single "Update" button.
 * Three consumers now need the same helpers, so they cannot live inside
 * any one of them :
 *
 *  - `components/settings/PluginUpdater.tsx`   — the panel header + button
 *  - `components/modals/PluginUpdateModal.tsx` — the controls themselves
 *  - `services/pluginUpdateNotice.tsx`         — the boot-time toast/badge
 *
 * Everything here is either a pure function (unit-testable for the first
 * time — see `plugin-update.test.ts`) or module-level state that must
 * outlive a React unmount. The comments on `getDeckyBackend`,
 * `resolveInstallAction` and the persisted-selection accessors each
 * record a bug that actually shipped; do not trim them.
 */
import { call } from "@decky/api";
import type { TFunction } from "i18next";
import { rpcRoutes } from "../api/rpc-routes";

/** One installable GitHub release, as returned by the updater RPCs. */
export interface ReleaseInfo {
  tag: string;
  version: string;
  prerelease: boolean;
  asset_url: string;
  asset_name: string;
  sha256: string;
  body: string;
}

/** Payload of `check_plugin_update` / `force_check_plugin_update`.
 *
 *  Previously written out inline at all three call sites (two queries and
 *  one mutation), which is exactly how they drift apart. */
export interface UpdateCheckResult {
  available: boolean;
  current: string;
  current_build_id: string | null;
  latest: ReleaseInfo | null;
}

// Decky PluginInstallType (backend enums.py / browser.py PluginInstallType)
export const INSTALL_TYPE_REINSTALL = 1;
export const INSTALL_TYPE_UPDATE = 2;
export const INSTALL_TYPE_DOWNGRADE = 3;

// window.DeckyBackend lives on whichever window actually created this
// document. In Desktop Mode's full-page Decky Settings route, that's
// this window directly. In Gaming Mode, this panel renders inside the
// Quick Access Menu's own popup window (opened via window.open by
// Big Picture Mode) — DeckyBackend is undefined on that popup's own
// `window`, but reachable via `window.opener`. Falling back silently
// means every install/update button appears to do nothing in Gaming
// Mode's QAM while working fine in Desktop Mode, which is how this
// went unnoticed.
//
// Verified still true (2026-08-23, live CDP probe of a Gaming Mode
// session): in the QuickAccess popup `typeof window.DeckyBackend` is
// "undefined" while `typeof window.opener.DeckyBackend` is "object".
// This MUST stay a function — resolving `window.opener` once at module
// init would capture the wrong (or a not-yet-existing) value.
export const getDeckyBackend = (): Window["DeckyBackend"] | null =>
  window.DeckyBackend ?? window.opener?.DeckyBackend ?? null;

// If Decky's own loader install dies silently (e.g. a 404 on a rotated
// dev-build asset — confirmed in journalctl: the browser CRITICAL "Could
// not fetch from URL" is followed by zero further progress/finish events),
// downloadActive would otherwise stay true forever. This is an inactivity
// timeout reset on every progress tick, not a single fixed deadline, so a
// legitimately slow ~40-50MB download over Wi-Fi isn't falsely flagged.
export const INSTALL_WATCHDOG_TIMEOUT_MS = 45_000;

// Parses the maintainer's dev-build filename convention (e.g.
// "unifideck.dev.0.7.1.g3f9a1c2.zip", or the legacy
// "unifideck.dev.v524.zip") into a display-friendly build id. Returns
// null when asset_name is absent or doesn't match — callers fall back
// to the generic "vDev" label in that case (e.g. a release built
// before this feature shipped, or a malformed manual upload).
const DEV_ASSET_NAME_RE = /^unifideck\.dev\.(.+)\.zip$/i;

/** Extract the build id baked into a dev release's asset filename. */
export const extractDevBuildId = (
  assetName: string | undefined,
): string | null => {
  if (!assetName) return null;
  const m = assetName.match(DEV_ASSET_NAME_RE);
  return m ? m[1] : null;
};

/** Numeric semver comparison. Returns 1 / 0 / -1. */
export const compareVersions = (a: string, b: string): number => {
  const parse = (v: string) => v.split(".").map((x) => parseInt(x, 10) || 0);
  const pa = parse(a);
  const pb = parse(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const na = pa[i] || 0;
    const nb = pb[i] || 0;
    if (na > nb) return 1;
    if (na < nb) return -1;
  }
  return 0;
};

/** What installing a given release means relative to what's running. */
export interface InstallAction {
  installType: number;
  displayVersion: string;
}

// Single source of truth for "is installing this release an update,
// downgrade, or reinstall relative to what's currently running" — used
// by both the install button's label and the actual install trigger,
// which used to each compute this independently and could disagree.
//
// Dev/prerelease releases are handled separately from stable ones:
// dev builds are deliberately cut BEFORE package.json's version bumps
// (see build-plugin.sh), so their parsed `version` is always the raw
// non-semver tag ("Dev-<date>-<time>-<sha>") — running that through the
// numeric compareVersions() above always parses to 0, which is <= any real
// release and would misreport EVERY dev install as a "downgrade"
// regardless of how new the underlying code actually is. There is no
// meaningful downgrade concept for a prerelease: it's a "Reinstall"
// only when its build id matches what's already running (currentBuildId),
// and an "Update" otherwise.
export const resolveInstallAction = (
  release: ReleaseInfo,
  currentVersion: string,
  currentBuildId: string | null,
): InstallAction => {
  if (release.prerelease) {
    const devBuildId = extractDevBuildId(release.asset_name);
    return {
      installType:
        devBuildId !== null && devBuildId === currentBuildId
          ? INSTALL_TYPE_REINSTALL
          : INSTALL_TYPE_UPDATE,
      displayVersion: devBuildId ?? release.version,
    };
  }
  const cmp = compareVersions(release.version, currentVersion);
  return {
    installType:
      cmp === 0
        ? INSTALL_TYPE_REINSTALL
        : cmp < 0
        ? INSTALL_TYPE_DOWNGRADE
        : INSTALL_TYPE_UPDATE,
    displayVersion: release.version,
  };
};

/** Map Decky's `download_progress_info.*` keys to short status text. */
export const stageLabel = (key: string | undefined, t: TFunction): string => {
  const suffix = (key ?? "").split(".").pop() ?? "";
  switch (suffix) {
    case "start":
      return t("updater.stageStart", { defaultValue: "Starting…" });
    case "download_zip":
    case "increment_count":
      return t("updater.stageDownload", { defaultValue: "Downloading…" });
    case "open_zip":
      return t("updater.stageOpen", { defaultValue: "Reading package…" });
    case "parse_zip":
      return t("updater.stageParse", { defaultValue: "Verifying…" });
    case "uninstalling_previous":
      return t("updater.stageRemove", {
        defaultValue: "Removing old version…",
      });
    case "installing_plugin":
      return t("updater.stageInstall", { defaultValue: "Installing…" });
    case "download_remote":
      return t("updater.stageFinish", { defaultValue: "Finishing…" });
    default:
      return t("updater.stageWorking", { defaultValue: "Installing…" });
  }
};

// Best-effort lifecycle logging into the Unifideck log dir (per-session).
// The backend may be mid-reload near the end of an install, so failures are ignored.
export const logEvent = (stage: string, detail: string): void => {
  void call<[string, string], unknown>(
    rpcRoutes.logUpdateEvent,
    stage,
    detail,
  ).catch(() => {});
};

// ── Persisted release selection ─────────────────────────────────────
//
// Selected release tag, persisted across mount/unmount. Two separate
// reasons it cannot be plain component state :
//
//  1. Opening the Dropdown's overlay dismounts the surrounding tree,
//     which would otherwise reset the selection to the default on every
//     pick. Mirrors `persistentActiveTab` in QuickAccessPanel.tsx.
//  2. The update modal is destroyed every time it closes, so without
//     this the choice would reset on each reopen.
//
// Cleared naturally when the plugin reloads after an install.
//
// Behind accessors rather than an exported `let`: ES module bindings are
// read-only for importers, so `import { tag } from …; tag = x` does not
// compile.
let persistentSelectedTag: string | null = null;

/** Last release tag the user picked, or null if they never have. */
export const getPersistedSelectedTag = (): string | null =>
  persistentSelectedTag;

/** Remember the picked release tag across unmounts. */
export const setPersistedSelectedTag = (tag: string | null): void => {
  persistentSelectedTag = tag;
};

// ── Pending-update badge state ──────────────────────────────────────
//
// Drives the dot on the plugin's `icon` and `titleView` (see
// components/PluginBadge.tsx). A module-level store rather than an RPC
// in the badge components themselves: Decky's plugin-list row mounts and
// unmounts every time the user opens or leaves the Decky tab, and the
// badge has to be right on the first frame — a fetch-on-mount would
// flash an un-badged icon each time.
//
// `services/pluginUpdateNotice.tsx` is the ONLY writer. Unlike the toast
// it publishes alongside, this is deliberately not frequency-capped: the
// dot stays lit for as long as an update is actually pending, and clears
// by itself once `available` goes false after the install.
//
// Kept as a plain store with no React import so this whole module stays
// unit-testable — React is peer-provided by the Steam webview and is not
// installed in node_modules, so vitest resolves it to a stub that has
// only `createElement`. The `useSyncExternalStore` wrapper therefore
// lives in components/PluginBadge.tsx instead.
let updatePending = false;
const updatePendingListeners = new Set<() => void>();

/** Publish whether an update is pending. Idempotent. */
export const setUpdatePending = (value: boolean): void => {
  if (value === updatePending) return;
  updatePending = value;
  updatePendingListeners.forEach((listener) => listener());
};

/** Current pending-update flag, for non-React callers. */
export const getUpdatePending = (): boolean => updatePending;

/** Register a change listener. Returns its unsubscribe. Stable identity,
 *  so it can be handed straight to `useSyncExternalStore`. */
export const subscribeUpdatePending = (listener: () => void): (() => void) => {
  updatePendingListeners.add(listener);
  return () => {
    updatePendingListeners.delete(listener);
  };
};
