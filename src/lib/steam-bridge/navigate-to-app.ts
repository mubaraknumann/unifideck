/**
 * navigateToApp — jump the focused Steam window to a specific app's
 * library detail page.
 *
 * `Navigation.Navigate(path)` (the generic `@decky/ui` helper) requires
 * a pre-registered route/alias — an arbitrary `/library/app/:id` path
 * silently bounces back to `/library/home` (confirmed live: the page
 * briefly showed the target app, then reverted). `@decky/ui`'s
 * `Navigation.NavigateToAppProperties` looked like the fix but opens the
 * Properties dialog (Launch Options / DLC / Betas), not the library page.
 *
 * The actual mechanism Steam's own library tiles use — found by
 * searching Steam's webpack bundle for the native tile's click handler
 * (`this.props.navigator.App(this.props.appid)`) — is `win.Navigator.
 * App(appid)` on the focused window instance. Neither `Navigator` nor
 * this method is declared in `@decky/ui`'s `Router.d.ts` (its
 * `WindowRouter`/`Navigation` types only cover `Navigate`,
 * `NavigateToAppProperties`, etc.), so this reaches it directly rather
 * than through the typed `Navigation` export.
 *
 * Mirrors `@decky/ui`'s own internal fallback chain for resolving a
 * window (get the focused window, else the main Gamepad UI window
 * instance) so this behaves the same whether a popup or the main window
 * currently has focus.
 *
 * `Navigator.App` resolves its argument against `appStore.m_mapApps`,
 * confirmed live (by dumping its keys for a real cross-store duplicate,
 * Fallout 3 GOTY on Epic + GOG) to be keyed by the **unsigned** 32-bit
 * form. The store switcher's `GroupSibling.appId` values, read live off
 * the same device via a React-fiber walk of the actually-rendered
 * component, turned out to be **signed** (`-584483551`, not the
 * `3710483745` the backend RPC name suggested) — so every call here was
 * silently matching no entry in `m_mapApps` and no-opping. `page` before
 * and after a real click on the live button confirmed the route never
 * changed; converting with `toUnsignedAppId` and retrying the exact same
 * appid live did navigate correctly. Convert once here so no caller has
 * to know which form its appid happens to be in.
 */
import { Router } from "@decky/ui";
import { toUnsignedAppId } from "./appid";

interface NavigatorWithApp {
  App: (appId: number) => void;
}
interface WindowWithNavigator {
  Navigator?: NavigatorWithApp;
  /** `Navigate(path, replace)`: the window's router call. With `replace`
   *  it swaps the current history entry instead of pushing one (measured
   *  on SteamOS 2026-10-03: history length unchanged, action "REPLACE",
   *  and a shortcut's unsigned `/library/app/<id>` route renders). */
  Navigate?: (path: string, replace?: boolean) => void;
}

export interface NavigateToAppOptions {
  /** Replace the current page instead of adding one. The store switcher
   *  uses it, so hopping between copies of a game never piles up pages
   *  that B has to walk back through. */
  replace?: boolean;
}

export function navigateToApp(
  appId: number,
  { replace = false }: NavigateToAppOptions = {},
): void {
  const steamUIStore = (
    window as unknown as {
      SteamUIStore?: { GetFocusedWindowInstance?: () => WindowWithNavigator };
    }
  ).SteamUIStore;

  let win: WindowWithNavigator | undefined;
  try {
    win = steamUIStore?.GetFocusedWindowInstance?.();
  } catch (e) {
    console.error(
      "[Unifideck] navigateToApp: GetFocusedWindowInstance failed",
      e,
    );
  }

  if (!win?.Navigator) {
    win = (
      Router as unknown as {
        WindowStore?: { GamepadUIMainWindowInstance?: WindowWithNavigator };
      }
    ).WindowStore?.GamepadUIMainWindowInstance;
  }

  if (!win?.Navigator) {
    console.error(
      "[Unifideck] navigateToApp: no window with a Navigator found",
    );
    return;
  }
  const unsigned = toUnsignedAppId(appId);
  if (replace && typeof win.Navigate === "function") {
    win.Navigate(`/library/app/${unsigned}`, true);
    return;
  }
  win.Navigator.App(unsigned);
}
