/**
 * Plugin entry — the thin lifecycle wiring.
 *
 * Replaces the 2409-line legacy index.tsx with ~130 LOC
 * that does exactly what a Decky plugin entry should do :
 *
 *   1. Mount <RootProvider> around <QuickAccessPanel>
 *   2. Register the App Details router patch
 *   3. Start boot-time singletons (stores, event listener)
 *   4. Run bootstrap tasks (language, account switch,
 *      lifetime listener)
 *   5. Return a teardown function for plugin unload
 *
 * That's it. Every other concern lives in F1-F5 :
 *  - SteamBridge isolates Steam internals
 *  - Contexts hold all reactive state
 *  - Hooks expose business actions
 *  - Components are pure presentational
 *  - Services drive the auth flows
 *
 * If this file grows past 200 LOC, something is being
 * smuggled in that should live in another layer. The size
 * is the safety net.
 */
import { definePlugin } from "@decky/api";
import { FC } from "react";
import { initI18n } from "./i18n";
import { SteamBridge } from "./lib/steam-bridge";
import { RootProvider } from "./contexts/RootProvider";
import { QuickAccessPanel } from "./views/QuickAccessPanel";
import { applyAppDetailsPatch } from "./views/AppDetailsPatch";
import { applyLibraryPatch } from "./lib/steam-bridge/library-patch";
import { startUnifideckCacheAutoload } from "./lib/library-filters";
import { startCollectionManager } from "./lib/steam-bridge/collection-manager";
import { startOverviewEnrichment } from "./lib/steam-bridge/overview-enrichment";
import { startTileStoreBadgePatch } from "./lib/steam-bridge/tile-store-badge-patch";
import { applyAppContextMenuPatch } from "./lib/steam-bridge/app-context-menu-patch";
import { applyAppStorePatch } from "./lib/steam-bridge/app-store-patcher";
import { loadCompatCacheFromBackend } from "./lib/protondb-cache";
import { runBootstrapTasks } from "./bootstrap-tasks";
import { startLauncherToastPoll } from "./services/launcherToasts";
import { startPluginUpdateNotice } from "./services/pluginUpdateNotice";
import { PluginIcon, PluginTitleView } from "./components/PluginBadge";
import { startBootEventListener } from "./services/boot-event-listener";
import { downloadStore } from "./stores/download-store";
import { syncStore } from "./stores/sync-store";
import { authStore } from "./stores/auth-store";
import { storeInfoStore } from "./stores/store-info-store";
import { runTeardown, type TeardownHandles } from "./teardown";
// Eager translation load — Decky's UI mounts before any
// async work resolves, so we kick this off at module import.
void initI18n();
/** Panel content. */
const PanelContent: FC = () => (
  <RootProvider>
    <QuickAccessPanel />
  </RootProvider>
);
export default definePlugin(() => {
  console.log("[Unifideck] Plugin loaded");
  const bridge = new SteamBridge();
  const handles: TeardownHandles = {};
  // Apply the App Details patch immediately so the very
  // first navigation to a game's page shows our overrides.
  try {
    handles.routerPatch = applyAppDetailsPatch(bridge);
  } catch (e) {
    console.error("[Unifideck] router patch failed:", e);
  }
  // Populate the unifideck game cache before patching tabs so
  // filters have data on first render — independent of whether
  // the user ever opens the Decky QAM panel.
  try {
    startUnifideckCacheAutoload();
  } catch (e) {
    console.error("[Unifideck] cache autoload start failed:", e);
  }
  // Load ProtonDB compat cache at boot so library tab patches
  // have compat badges on first render (no QAM open required).
  void loadCompatCacheFromBackend();
  // Inject the custom Unifideck tabs into Steam's library via
  // the ``/library`` route patch — same primitive TabMaster uses
  // (works in both Desktop and Gaming Mode).
  try {
    handles.libraryPatch = applyLibraryPatch(bridge);
  } catch (e) {
    console.error("[Unifideck] library patch failed:", e);
  }
  // Auto-generate [Unifideck] Steam Collections + keep them
  // in sync with the tab filters on every library sync.
  try {
    handles.collectionManager = startCollectionManager();
  } catch (e) {
    console.error("[Unifideck] collection manager start failed:", e);
  }
  // Enrich non-Steam shortcut AppOverviews (metacritic, deck compat,
  // store categories, release date, reviews, date-added, playtime,
  // size) so Steam's NATIVE library Sort menu + Library Filters work
  // for them. Runs at boot — NOT on QAM mount — so it works in Gaming
  // Mode, and re-applies on every overview re-set so closing a game's
  // details page doesn't wipe the enrichment.
  try {
    handles.overviewEnrichment = startOverviewEnrichment();
  } catch (e) {
    console.error("[Unifideck] overview enrichment start failed:", e);
  }
  // Render each non-Steam shortcut's STORE logo (Epic/GOG/…) on its
  // library tile in place of the Steam Deck 'D' glyph, keeping the
  // compat status badge. Boot-time (before the grid mounts) so it works
  // in Gaming Mode. Read-only w.r.t. overviews → launch unaffected.
  try {
    handles.tileStoreBadgePatch = startTileStoreBadgePatch();
  } catch (e) {
    console.error("[Unifideck] tile store-badge patch start failed:", e);
  }
  // Inject "Change executable…" into the native game context menu for
  // installed Unifideck shortcuts (gog/amazon/epic). Read-only w.r.t. the
  // overview — it only ADDS a menu item, never touches launch routing.
  try {
    handles.appContextMenuPatch = applyAppContextMenuPatch();
  } catch (e) {
    console.error("[Unifideck] app context-menu patch start failed:", e);
  }
  // ── Boot-time singletons ──────────────────────────────
  // Start all reactive stores at boot so they track state
  // even when the QAM panel is closed. Each store subscribes
  // to EventBus events and/or fetches initial data.
  authStore.start();
  storeInfoStore.start();
  downloadStore.start();
  syncStore.start();
  // Boot-time event listener — handles STORE_ERROR, LAUNCHER_STAGE,
  // and STORE_AUTH_COMPLETE events independently of QAM mount.
  try {
    handles.bootEventListener = startBootEventListener();
  } catch (e) {
    console.error("[Unifideck] boot event listener start failed:", e);
  }
  // Persistent poll for launcher-subprocess toasts (first-time prefix
  // setup, dependency install, Proton switch). Runs here — NOT in the
  // QAM panel — so launch-time toasts show in Gaming Mode where the
  // panel is closed during the launch.
  try {
    handles.launcherToastPoll = startLauncherToastPoll();
  } catch (e) {
    console.error("[Unifideck] launcher toast poll start failed:", e);
  }
  // Announce a new plugin release: a capped toast plus the durable dot on
  // our QAM icon/title. Boot-time — the backend's 6-hour poller only ever
  // wrote a log line, so before this the only way to notice an update was
  // to open the QAM and read the version header.
  try {
    handles.pluginUpdateNotice = startPluginUpdateNotice();
  } catch (e) {
    console.error("[Unifideck] plugin update notice start failed:", e);
  }
  // Spoof non-Steam Unifideck shortcuts as Steam Store games so
  // Steam's own UI surfaces (library tile, AppDetails page,
  // friend presence) render real cover art + descriptions instead
  // of the bare shortcut skeleton. Async — needs to fetch the
  // mappings + appdetails caches from the backend before patching.
  void applyAppStorePatch()
    .then((handle) => {
      handles.appStorePatch = handle;
    })
    .catch((e) => {
      console.error("[Unifideck] app-store patch failed:", e);
    });
  // Bootstrap tasks (language, account switch, lifetime
  // listener) run async ; the lifetime listener handle is
  // captured for teardown when its promise resolves.
  void runBootstrapTasks().then((listener) => {
    handles.lifetimeListener = listener;
  });
  return {
    name: "Unifideck",
    // Components, not static nodes: Decky renders both of these in the
    // QAM (the plugin-list row and the open-plugin header), so they are
    // where the pending-update dot lives. See components/PluginBadge.tsx
    // for why Decky's own `_updates` badge is not used.
    titleView: <PluginTitleView />,
    content: <PanelContent />,
    icon: <PluginIcon />,
    onDismount: () => {
      console.log("[Unifideck] Plugin unloading");
      runTeardown(handles);
    },
  };
});
