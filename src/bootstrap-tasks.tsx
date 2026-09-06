/**
 * Bootstrap tasks — one-shot startup work for the plugin.
 *
 * Runs in the plugin entry's `definePlugin` callback. Each
 * task is fire-and-forget — none of them block UI rendering.
 * Failure of one task never blocks the others (each runs in
 * its own try/catch).
 *
 * Tasks :
 *  - applyLanguagePreference  : reads backend language pref
 *    and switches i18next to it (default = navigator lang).
 *  - checkAccountSwitch       : detects Steam account change
 *    and shows the migration modal if needed (legacy RPCs
 *    `check_account_switch` + `migrate_account_data`).
 *  - registerLifetimeListener : global app lifetime hook for
 *    playtime tracking — calls `notify_game_launched` /
 *    `notify_game_stopped` on every Steam app start/stop.
 *
 * Replaces the ~600 lines of inline startup code from the
 * legacy index.tsx (lines 1960-1995 for the account switch
 * flow specifically).
 */
import { call } from "@decky/api";
import { showModal } from "@decky/ui";
import i18n from "i18next";
import { rpcRoutes } from "./api/rpc-routes";
import { unwrapRpcEnvelope } from "./api/useRPC";
import {
  AUTO_DETECT,
  LANG_STORAGE_KEY,
  resolveAutoLanguage,
} from "./i18n/translations";
import { AccountSwitchModal, SteamRestartModal } from "./components/modals";
import { uploadSteamOwnedTitles } from "./lib/steam-bridge/owned-library";
import { uploadActiveSteamUser } from "./lib/steam-bridge/active-user";
import { loadDeviceType } from "./lib/device-type";
import { tabManager } from "./lib/steam-bridge/tab-container";
import type { Unregisterable } from "./types/steam";
/** Language pref — the `data` payload of `get_language_preference`
 *  after the `{success, error, data}` envelope is unwrapped.
 *  `locale` is the stored preference ("auto" or a concrete tag). */
interface LanguagePref {
  locale: string;
}
/** Account switch info. */
interface AccountSwitchInfo {
  show_modal: boolean;
  has_registry: boolean;
  has_auth_tokens: boolean;
}
/** Migrate result. */
interface MigrateResult {
  shortcuts_created: number;
  artwork_copied: number;
}
/**
 * Bootstrap task : push the persisted UI language
 * to i18next and to the backend before the rest of
 * the tree mounts, so toasts and labels are already
 * localised on the very first render.
 *
 * @returns a promise that resolves once both sinks
 *   have acknowledged the new language.
 */
export async function applyLanguagePreference(): Promise<void> {
  try {
    // get_language_preference returns the {success, error, data}
    // envelope — unwrap to the {locale} payload (raw `call` doesn't).
    const raw = await call<[], unknown>(rpcRoutes.getLanguagePreference);
    const r = unwrapRpcEnvelope<LanguagePref>(raw, {
      route: rpcRoutes.getLanguagePreference,
      throwing: false,
    });
    const pref = r?.locale || AUTO_DETECT;
    // "auto" (or an empty pref) resolves to the detected tag —
    // i18next has no "auto" bundle and would fall back to English.
    const tag = pref === AUTO_DETECT ? resolveAutoLanguage() : pref;
    if (i18n.language !== tag) {
      await i18n.changeLanguage(tag);
    }
    // Mirror the preference so the early toast path and
    // <LocaleProvider> agree on what's selected.
    try {
      localStorage.setItem(LANG_STORAGE_KEY, pref);
    } catch {
      // ignore quota/availability errors
    }
  } catch {
    // Backend not ready — safe default (navigator language) stays
  }
}
/**
 * Bootstrap task : compare the active Steam user
 * with the one Unifideck last ran under. On
 * mismatch, emit `ACCOUNT_SWITCHED` and clear
 * cross-account caches so we don't leak the previous
 * user's library / artwork.
 *
 * @returns a promise resolving once the check (and
 *   any required cache clear) is done.
 */
/**
 * Bootstrap task : resolve the device class and re-title the
 * compatibility tab to name the actual hardware.
 *
 * Rebuilds only when the value actually changed. Note the cached
 * default is `"other"`, not `"deck"` — so on a real Deck this DOES
 * change once at boot and costs one rebuild. That is deliberate: a
 * device whose RPC never answers then shows the neutral SteamOS
 * wording rather than confidently claiming to be a Deck, which on a
 * Steam Machine would be a wrong device name.
 */
export async function applyDeviceType(): Promise<void> {
  try {
    const changed = await loadDeviceType();
    if (changed && tabManager.isInitialized()) {
      tabManager.rebuildTabs();
    }
  } catch {
    // Non-fatal — the neutral "other" default label stays.
  }
}

export async function checkAccountSwitch(): Promise<void> {
  try {
    const r = await call<[], AccountSwitchInfo>(rpcRoutes.checkAccountSwitch);
    if (!r?.show_modal) return;
    showModal(
      <AccountSwitchModal
        hasRegistry={r.has_registry}
        hasAuthTokens={r.has_auth_tokens}
        onMigrate={async () => {
          await call<[], MigrateResult>(rpcRoutes.migrateAccountData);
          showModal(<SteamRestartModal />);
        }}
        onClearAuths={async () => {
          await call<[], unknown>(rpcRoutes.clearStoreAuths);
        }}
        onSkip={() => {}}
        closeModal={() => {}}
      />,
    );
  } catch {
    // Silently ignore — never block plugin load
  }
}
/**
 * Bootstrap task : install the singleton Steam
 * lifetime listener that drives the Unifideck game
 * runner. Returns the disposer used by
 * `runTeardown` on plugin shutdown.
 *
 * @returns a disposer that detaches the listener.
 */
export function registerLifetimeListener(): Unregisterable | null {
  try {
    return (
      window.SteamClient?.GameSessions?.RegisterForAppLifetimeNotifications?.(
        (n) => onAppLifetime(n),
      ) ?? null
    );
  } catch (e) {
    console.error("[Bootstrap] lifetime listener registration failed:", e);
    return null;
  }
}
/** On app lifetime. */
function onAppLifetime(n: {
  unAppID: number;
  bRunning: boolean;
  nInstanceID: number;
}): void {
  if (n.bRunning) {
    void call(rpcRoutes.notifyGameLaunched, n.unAppID).catch(() => {});
  } else {
    void call(rpcRoutes.notifyGameStopped, n.unAppID).catch(() => {});
  }
}
/**
 * Bootstrap task : sweep any persistent OAuth auth
 * shortcut left over in Steam's in-memory app store by
 * a previous plugin version. Today's auth flow only
 * uses ephemeral shortcuts (created and removed in one
 * connect cycle); a stale persistent row would otherwise
 * keep launching with cover art + a real-game tile
 * instead of the unifideck-launcher tile. Idempotent —
 * RemoveShortcut on an unknown appid is a no-op.
 */
export function purgeLeftoverAuthShortcuts(): void {
  try {
    const appStore = (
      window as unknown as {
        appStore?: {
          m_mapApps?: {
            forEach?: (
              cb: (
                app: {
                  LaunchOptions?: unknown;
                  launch_options?: unknown;
                  display_name?: unknown;
                  appname?: unknown;
                },
                id: number,
              ) => void,
            ) => void;
          };
        };
      }
    ).appStore;
    const map = appStore?.m_mapApps;
    if (!map?.forEach) return;
    const stalePrefixes = [
      "epic:epic-auth",
      "gog:gog-auth",
      "amazon:amazon-auth",
      "microsoft:ms-auth",
    ];
    // No ownership gate is possible here: `m_mapApps` entries carry no
    // Exe/target field, so unlike every backend sweep this one cannot
    // prove a shortcut is ours. The four prefixes are specific enough
    // that the residual risk is small, but log the name we are about to
    // remove so the action is auditable from a support bundle.
    const victims: { appId: number; name: unknown }[] = [];
    map.forEach((app, appId) => {
      const lo = app?.LaunchOptions ?? app?.launch_options;
      if (typeof lo !== "string") return;
      if (stalePrefixes.some((p) => lo.startsWith(p))) {
        victims.push({ appId, name: app?.display_name ?? app?.appname });
      }
    });
    const steamApps = window.SteamClient?.Apps;
    if (!steamApps?.RemoveShortcut) return;
    for (const { appId, name } of victims) {
      console.log(
        `[Bootstrap] Removing leftover persistent auth shortcut ` +
          `appId=${appId} name=${JSON.stringify(name ?? null)}`,
      );
      try {
        steamApps.RemoveShortcut(appId);
      } catch (e) {
        console.error(`[Bootstrap] RemoveShortcut(${appId}) failed:`, e);
      }
    }
  } catch (e) {
    console.error("[Bootstrap] purgeLeftoverAuthShortcuts failed:", e);
  }
}
/** One entry of the `delete` list from `scan_orphaned_shortcuts`. */
interface OrphanDeleteEntry {
  appid_unsigned: number;
  name?: string;
}
/** One entry of the `recover` list from `scan_orphaned_shortcuts`. */
interface OrphanRecoverEntry {
  appid_unsigned: number;
  store?: string;
  game_id?: string;
  full_id?: string;
  name?: string;
}
/** `data` payload of `scan_orphaned_shortcuts` (post-envelope). */
interface OrphanScanResult {
  delete: OrphanDeleteEntry[];
  recover: OrphanRecoverEntry[];
  launcher_path: string;
}
/**
 * Bootstrap task : sweep orphaned Unifideck shortcuts that the
 * post-sync reconcile can't see — it keys off `LaunchOptions`,
 * never the `Exe`/target field, so a shortcut with our launcher
 * as its target but empty launch options lingers forever.
 *
 * The backend reads shortcuts.vdf (the only place that has both
 * fields) and classifies orphans; here we act on them live :
 *  - `delete` (Type A : our launcher target, no valid launch
 *    options) → `RemoveShortcut`, live, no Steam restart. These
 *    are unrecoverable (we can't tell which game they were).
 *  - `recover` (Type B : valid launch options, missing/foreign
 *    target) → logged only. The frontend has no `SetShortcutExe`
 *    to fix the target live; the next library sync's reconcile
 *    restores it (in-library) or sweeps it (out-of-library).
 *
 * Best-effort and idempotent — `RemoveShortcut` on an unknown
 * appid is a no-op, and once Steam persists the deletion a re-run
 * finds nothing.
 */
export async function sweepOrphanedShortcuts(): Promise<void> {
  try {
    const raw = await call<[], unknown>(rpcRoutes.scanOrphanedShortcuts);
    const r = unwrapRpcEnvelope<OrphanScanResult>(raw, {
      route: rpcRoutes.scanOrphanedShortcuts,
      throwing: false,
    });
    if (!r) return;
    const toRecover = Array.isArray(r.recover) ? r.recover : [];
    for (const entry of toRecover) {
      console.log(
        `[Bootstrap] Orphaned shortcut with recoverable launch options ` +
          `appId=${entry.appid_unsigned} (${entry.full_id ?? "?"}) — ` +
          `target fix deferred to next library sync`,
      );
    }
    const toDelete = Array.isArray(r.delete) ? r.delete : [];
    if (toDelete.length === 0) return;
    const steamApps = window.SteamClient?.Apps;
    if (!steamApps?.RemoveShortcut) return;
    for (const entry of toDelete) {
      const appId = entry.appid_unsigned;
      if (typeof appId !== "number") continue;
      console.log(
        `[Bootstrap] Removing orphaned shortcut appId=${appId} ` +
          `(${entry.name || "unnamed"})`,
      );
      try {
        steamApps.RemoveShortcut(appId);
      } catch (e) {
        console.error(`[Bootstrap] RemoveShortcut(${appId}) failed:`, e);
      }
    }
  } catch (e) {
    console.error("[Bootstrap] sweepOrphanedShortcuts failed:", e);
  }
}
/** Run all bootstrap tasks concurrently. Returns the
 *  unregister handle for the lifetime listener so the
 *  plugin entry can call it on unload. */
export async function runBootstrapTasks(): Promise<Unregisterable | null> {
  purgeLeftoverAuthShortcuts();
  // Push the live logged-in Steam user to the backend FIRST — it's the only
  // 100%-correct source of the active account, and everything below (account-
  // switch check, owned-titles, any sync) must target the right userdata dir.
  await uploadActiveSteamUser();
  const [, , , , , listener] = await Promise.all([
    applyLanguagePreference(),
    // Name the compatibility tab after the actual hardware. Defaults to
    // "deck", so a failure here leaves the pre-existing label rather
    // than blanking the tab.
    applyDeviceType(),
    checkAccountSwitch(),
    // Seed the owned-Steam-library snapshot early so a backend-triggered
    // (auto) sync can hide Steam-linked Ubisoft games before the user's
    // first manual sync. Best effort; refreshed again before each sync.
    uploadSteamOwnedTitles(),
    // Remove orphaned Unifideck shortcuts (launcher target, empty launch
    // options) that reconcile is blind to. Disjoint from the auth-shortcut
    // purge above (auth shortcuts are protected backend-side), so safe to
    // run concurrently.
    sweepOrphanedShortcuts(),
    Promise.resolve(registerLifetimeListener()),
  ]);
  return listener;
}
