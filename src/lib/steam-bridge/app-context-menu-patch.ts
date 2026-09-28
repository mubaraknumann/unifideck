/**
 * app-context-menu-patch — inject "Change executable…" and "Companion
 * executables…" into the native game context menu (the gear / right-click
 * menu with Add to Favorites, Manage, Properties…).
 *
 * Technique ported from decky-steamgriddb's `contextMenuPatch.tsx`: resolve the
 * `LibraryContextMenu` component, `afterPatch` its `render` (+ the inner
 * `type.render` / `shouldComponentUpdate`), and splice a `MenuItem` in just
 * before the "Properties…" entry. Proven robust across Steam client versions.
 *
 * GATING:
 * - "Change executable…" is added only for an INSTALLED Unifideck shortcut
 *   whose store supports an executable override (gog / amazon / epic /
 *   gamevault — see `SUPPORTED_STORES`).
 * - "Companion executables…" is added for ANY installed Unifideck shortcut
 *   (it does not touch a store's games.map exe column — see
 *   `companionsEligible`).
 * Regular (non-Unifideck) Steam games are left untouched either way. The
 * patch only ADDS menu items; it never mutates the overview or launch
 * routing.
 */
import {
  afterPatch,
  fakeRenderComponent,
  findInReactTree,
  findInTree,
  findModuleByExport,
  MenuItem,
  showModal,
  type Patch,
} from "@decky/ui";
import { createElement } from "react";
import i18n from "i18next";
import { getUnifideckGame } from "../library-filters";
import { ChangeExecutableModal } from "../../components/modals/ChangeExecutableModal";
import { CompanionExecutablesModal } from "../../components/modals/CompanionExecutablesModal";

/** Stores whose launch target the user can override (see ExecutableRPCMixin). */
const SUPPORTED_STORES = new Set(["gog", "amazon", "epic", "gamevault"]);

/** Stable key so re-renders can dedupe our injected item. */
const MENU_ITEM_KEY = "unifideck-change-exe";
/** Stable key for the "Companion executables…" item (see CompanionExecutablesRPCMixin). */
const COMPANIONS_MENU_ITEM_KEY = "unifideck-companion-exes";

export interface AppContextMenuPatchHandle {
  unpatch: () => void;
}

/** A Unifideck shortcut that supports an exe override, or null. */
function eligible(appId: number): {
  store: string;
  gameId: string;
} | null {
  const game = getUnifideckGame(appId);
  if (
    !game ||
    !game.storeGameId ||
    !game.isInstalled ||
    !SUPPORTED_STORES.has(game.store)
  ) {
    return null;
  }
  return { store: game.store, gameId: game.storeGameId };
}

function openModal(appId: number): void {
  const g = eligible(appId);
  if (!g) return;
  const overview = (
    window as unknown as {
      appStore?: {
        GetAppOverviewByAppID?: (
          id: number,
        ) => { display_name?: string } | null;
      };
    }
  ).appStore?.GetAppOverviewByAppID?.(appId);
  const title = overview?.display_name ?? g.gameId;
  showModal(
    createElement(ChangeExecutableModal, {
      store: g.store,
      gameId: g.gameId,
      gameTitle: String(title),
      closeModal: () => {},
    }),
  );
}

/** The game's display name from Steam's app store, falling back to the id. */
function resolveTitle(appId: number, gameId: string): string {
  const overview = (
    window as unknown as {
      appStore?: {
        GetAppOverviewByAppID?: (
          id: number,
        ) => { display_name?: string } | null;
      };
    }
  ).appStore?.GetAppOverviewByAppID?.(appId);
  return String(overview?.display_name ?? gameId);
}

/** Every store may attach companion executables — unlike "Change
 *  executable…" this isn't gated by ``SUPPORTED_STORES`` since it doesn't
 *  touch the games.map exe column at all, so xCloud/Microsoft games are
 *  eligible too, provided they're an installed Unifideck shortcut. */
function companionsEligible(
  appId: number,
): { store: string; gameId: string } | null {
  const game = getUnifideckGame(appId);
  if (!game || !game.storeGameId || !game.isInstalled) return null;
  return { store: game.store, gameId: game.storeGameId };
}

function openCompanionsModal(appId: number): void {
  const g = companionsEligible(appId);
  if (!g) return;
  const title = resolveTitle(appId, g.gameId);
  showModal(
    createElement(CompanionExecutablesModal, {
      store: g.store,
      gameId: g.gameId,
      gameTitle: title,
      closeModal: () => {},
    }),
  );
}

/** Insert our items before "Properties…" (matched by its onSelected source). */
function spliceItem(children: unknown[], appId: number): void {
  const propsIdx = children.findIndex((item) =>
    findInReactTree(
      item,
      (x: { onSelected?: { toString(): string } }) =>
        !!x?.onSelected && x.onSelected.toString().includes("AppProperties"),
    ),
  );
  const nodes: unknown[] = [];
  if (eligible(appId)) {
    nodes.push(
      createElement(
        MenuItem,
        { key: MENU_ITEM_KEY, onSelected: () => openModal(appId) },
        i18n.t("play.exe.menuItem"),
      ),
    );
  }
  if (companionsEligible(appId)) {
    nodes.push(
      createElement(
        MenuItem,
        {
          key: COMPANIONS_MENU_ITEM_KEY,
          onSelected: () => openCompanionsModal(appId),
        },
        i18n.t("play.companions.menuItem"),
      ),
    );
  }
  if (propsIdx >= 0) children.splice(propsIdx, 0, ...nodes);
  else children.push(...nodes);
}

/** Drop a previously-injected item so a re-render can't duplicate it. */
function dedupe(children: unknown[]): void {
  for (const key of [MENU_ITEM_KEY, COMPANIONS_MENU_ITEM_KEY]) {
    const idx = children.findIndex(
      (x) => (x as { key?: string } | null)?.key === key,
    );
    if (idx !== -1) children.splice(idx, 1);
  }
}

/** Re-resolve the appid from the menu's OWN React tree (not a stale closure).
 *
 * The outer-render `appId` is captured once (the first menu ever opened), so
 * trusting it would make every game's menu act on that first game. Mirror
 * decky-steamgriddb: prefer a child whose owner carries a *different* overview
 * appid, then any `app.appid` in the tree, falling back to the closure value.
 */
function resolveItemsAppId(
  menuItems: unknown[],
  fallbackAppId: number,
): number {
  const items = menuItems as Array<{
    _owner?: { pendingProps?: { overview?: { appid?: number } } };
  }>;
  const parent = items.find((x) => {
    const a = x?._owner?.pendingProps?.overview?.appid;
    return !!a && a !== fallbackAppId;
  });
  const parentAppId = parent?._owner?.pendingProps?.overview?.appid;
  if (parentAppId) return parentAppId;
  const found = findInTree(
    menuItems,
    (x: { app?: { appid?: number } }) => !!x?.app?.appid,
    { walkable: ["props", "children"] },
  );
  return found?.app?.appid ?? fallbackAppId;
}

/** Dedupe, re-resolve the per-menu appid, and (if eligible) splice our item.
 *
 * This is the single choke point for adding the item — every patched render
 * path routes through here so the appid is ALWAYS resolved from the live menu
 * (fixing the "every game shows Fallout NV" stale-closure bug).
 */
function patchMenuItems(menuItems: unknown[], fallbackAppId: number): void {
  dedupe(menuItems);
  const appId = resolveItemsAppId(menuItems, fallbackAppId);
  if (!eligible(appId) && !companionsEligible(appId)) return;
  spliceItem(menuItems, appId);
}

/** Heuristic that this is the app context menu (vs a screenshot/other menu). */
function isAppContextMenu(items: unknown): boolean {
  if (!Array.isArray(items) || !items.length) return false;
  return !!findInReactTree(
    items,
    (x: { props?: { onSelected?: { toString(): string } } }) =>
      !!x?.props?.onSelected &&
      x.props.onSelected.toString().includes("launchSource"),
  );
}

/** Resolve the menu's appid across client versions (overview → app.appid). */
function resolveAppId(component: {
  _owner?: { pendingProps?: { overview?: { appid?: number } } };
  props?: { children?: unknown };
}): number {
  const fromOwner = component?._owner?.pendingProps?.overview?.appid;
  if (fromOwner) return fromOwner;
  const found = findInTree(
    component?.props?.children,
    (x: { app?: { appid?: number } }) => !!x?.app?.appid,
    { walkable: ["props", "children"] },
  );
  return found?.app?.appid ?? 0;
}

/** The `LibraryContextMenu` class component, or null if Steam changed it. */
function resolveLibraryContextMenu(): {
  prototype: Record<string, unknown>;
} | null {
  try {
    const mod = findModuleByExport(
      (e: { toString?: () => string }) =>
        !!e?.toString && e.toString().includes("().LibraryContextMenu"),
    );
    const sibling = Object.values(mod ?? {}).find(
      (s: unknown) =>
        typeof (s as { toString?: () => string })?.toString === "function" &&
        (s as { toString: () => string }).toString().includes("navigator:"),
    );
    if (!sibling) return null;
    return fakeRenderComponent(sibling as () => unknown).type ?? null;
  } catch (e) {
    console.error("[Unifideck] LibraryContextMenu resolve failed:", e);
    return null;
  }
}

/**
 * Patch the native game context menu to add "Change executable…". Returns a
 * handle whose `unpatch()` removes it (called on plugin teardown). A no-op
 * handle is returned if Steam's menu component can't be located.
 */
export function applyAppContextMenuPatch(): AppContextMenuPatchHandle {
  const LibraryContextMenu = resolveLibraryContextMenu();
  if (!LibraryContextMenu) {
    return { unpatch: () => undefined };
  }

  let inner: Patch | undefined;
  const outer = afterPatch(
    LibraryContextMenu.prototype,
    "render",
    (_args: unknown[], component: unknown) => {
      // Fallback appid for THIS render; the real one is re-resolved per-menu
      // inside patchMenuItems (the closure value goes stale across opens).
      const appId = resolveAppId(
        component as Parameters<typeof resolveAppId>[0],
      );
      if (!inner) {
        inner = afterPatch(component, "type", (_a: unknown[], ret: unknown) => {
          const proto = (
            ret as { type?: { prototype?: Record<string, unknown> } }
          )?.type?.prototype;
          if (!proto) return ret;
          afterPatch(proto, "render", (_b: unknown[], ret2: unknown) => {
            const menuItems = (ret2 as { props?: { children?: unknown[] } })
              ?.props?.children?.[0];
            if (!isAppContextMenu(menuItems)) return ret2;
            try {
              patchMenuItems(menuItems as unknown[], appId);
            } catch (e) {
              console.error("[Unifideck] context-menu splice failed:", e);
            }
            return ret2;
          });
          afterPatch(
            proto,
            "shouldComponentUpdate",
            (args: unknown[], shouldUpdate: unknown) => {
              const next = (args?.[0] as { children?: unknown })?.children;
              if (Array.isArray(next)) {
                try {
                  dedupe(next); // always clear stale, even when not updating
                  if (shouldUpdate === true) patchMenuItems(next, appId);
                } catch {
                  /* wrong menu — leave it */
                }
              }
              return shouldUpdate;
            },
          );
          return ret;
        });
      } else {
        // Subsequent opens: the inner patch is bound to the FIRST menu's
        // component prototype and may not fire for this one — splice directly
        // so the item reliably appears (fixes the "randomly disappears" bug).
        try {
          const children = (component as { props?: { children?: unknown } })
            ?.props?.children;
          if (Array.isArray(children)) patchMenuItems(children, appId);
        } catch (e) {
          console.error("[Unifideck] context-menu else-splice failed:", e);
        }
      }
      return component;
    },
  );

  return {
    unpatch: () => {
      try {
        outer?.unpatch();
        inner?.unpatch();
      } catch (e) {
        console.error("[Unifideck] context-menu unpatch failed:", e);
      }
    },
  };
}
