# UI Injection Guide — Unifideck (Steam Deck / Decky Loader)

This document describes the UI injection methods used by Unifideck. The React-tree component injection (§1) is current. **Section 2 (native Play-button hiding) has been superseded:** the async CDP hide was removed in 2026-06 in favour of a synchronous scoped-CSS marker — see the note at the top of §2.

_Last reviewed: 2026-06-22._

---

## Architecture Overview

Steam Deck UI runs in **Chromium Embedded Framework (CEF)**. Decky plugins execute in a
separate CEF process (`about:blank?createflags=…`), while Steam's library UI renders in
the **SP (Steam Platform) tab** (`steamloopback.host`). This process boundary means:

| Approach                                          | Works? | Why                                                         |
| ------------------------------------------------- | ------ | ----------------------------------------------------------- |
| `document.createElement()` / DOM manipulation     | ❌     | Creates elements in the wrong process                       |
| `ReactDOM.createPortal()`                         | ❌     | Portal target not accessible cross-process                  |
| `routerHook.addPatch()` + `React.createElement()` | ✅     | Mutates React tree before reconciliation in Steam's process |
| CDP (Chrome DevTools Protocol) JS injection       | ✅     | Executes JS directly in Steam's SP tab via WebSocket        |

---

## 1. React Tree Patching (Component Injection)

Used for: **PlaySectionWrapper**, **GameInfoPanel**, **InstallInfoDisplay**

### Pattern

```typescript
import { routerHook, call } from "@decky/api";
import {
  afterPatch,
  findInReactTree,
  createReactTreePatcher,
  appDetailsClasses,
  playSectionClasses,
} from "@decky/ui";

routerHook.addPatch("/library/app/:appid", (routerTree) => {
  const routeProps = findInReactTree(routerTree, (x) => x?.renderFunc);
  const patchHandler = createReactTreePatcher(
    [
      (tree) =>
        findInReactTree(tree, (x) => x?.props?.children?.props?.overview)?.props
          ?.children,
    ],
    (_, ret) => {
      const overview = findInReactTree(
        ret,
        (x) => x?.props?.children?.props?.overview,
      )?.props?.children?.props?.overview;
      const appId = overview.appid;

      const container = findInReactTree(
        ret,
        (x) =>
          Array.isArray(x?.props?.children) &&
          x?.props?.className?.includes(appDetailsClasses?.InnerContainer),
      );

      // Splice components into the children array
      container.props.children.splice(
        index,
        0,
        React.createElement(MyComponent, { appId }),
      );
      return ret;
    },
  );
  afterPatch(routeProps, "renderFunc", patchHandler);
  return routerTree;
});
```

### InnerContainer Children Order (Native)

| Index | Component     | Description                                                 |
| ----- | ------------- | ----------------------------------------------------------- |
| 0     | HeaderCapsule | Hero image / top capsule area                               |
| 1     | PlaySection   | Native Play/Install button row                              |
| 2     | AboutThisGame | Tabbed content (ACTIVITY, YOUR STUFF, COMMUNITY, GAME INFO) |

### Injection Positions (Unifideck)

For non-Steam (Unifideck) games, components use **anchor-based insertion** (not hardcoded indices):

| Component          | Positioned After   | Strategy                                                        |
| ------------------ | ------------------ | --------------------------------------------------------------- |
| PlaySectionWrapper | Native PlaySection | `findPlaySectionInsertIndex()` — finds native PlaySection child |
| GameInfoPanel      | PlaySectionWrapper | `findIndex()` on PlaySectionWrapper key, insert after           |
| InstallInfoDisplay | GameInfoPanel      | `findIndex()` on GameInfoPanel key, insert after                |

**Container requirement**: Non-Steam games MUST use InnerContainer. If not found (partial tree,
timing), injection is skipped — the patcher retries on the next React render cycle. This prevents
elements from being injected into wrong fallback containers with different children structures.

**Position correction**: On patcher re-runs, if components are found at wrong positions (e.g.,
drifted to the bottom after restart), they are automatically repositioned.

For native Steam games, only InstallInfoDisplay is injected at index 2 (fallback containers OK).

### Deduplication

React re-renders trigger the patcher multiple times. Guard with key-based checks:

```typescript
const key = `unifideck-play-wrapper-${appId}`;
const alreadyHas = container.props.children.some((c) => c?.key === key);
if (!alreadyHas) {
  /* splice */
}
```

---

## 2. Native Play Button Hiding

> **⚠️ Superseded (2026-06).** The CDP / DOM-manipulation approach described in the rest of this section is **no longer used**. The RPC methods `hide_native_play_section` / `unhide_native_play_section` and the frontend `chainCDPOp`/`pendingCDPOps` plumbing have been **removed**.
>
> **Current approach (synchronous, no flash):** during the `AppDetailsPatch` React-tree patch, the `InnerContainer` is tagged with the marker class `HIDE_NATIVE_PLAY_MARKER` (`"unifideck-hide-native-play"`, defined in `src/components/play/play.css.ts`). `PlaySectionWrapper` renders `<style>{nativeAppDetailsHideCss()}</style>`, whose rules hide the native row **only inside a marked container**. Because the marker and the style are applied in the same synchronous render pass, there is no async round-trip and no flash of the native UI. The marker is only applied for managed (non-Steam) shortcuts, so native Steam app details are left untouched.
>
> **Mode gotcha:** desktop vs Gaming-Mode (BPM/gamepadui) use different AppDetails class names — `nativeAppDetailsHideCss()` emits a rule for both (`playSectionClasses.Container` desktop; `basicAppDetailsSectionStylerClasses.PlaySection` / `.AppDetailsContainer` for the Deck).
>
> The CDP material below is retained as a general reference for cross-process CSS/DOM injection (still relevant for the xCloud streaming flows in `py_modules/unifideck/cdp/`), but it does **not** reflect how the play row is hidden today.

### Legacy: Why CDP was used

- `@decky/ui`'s `playSectionClasses.PlayBar` resolves to a CSS class (e.g. `_3fLo166MlaNqP8r8tTyRz`)
  that **does not match any DOM element** — the class mappings are stale/outdated.
- CSS injection with those class names silently fails (0 elements matched).
- Direct DOM manipulation from the plugin process fails (wrong CEF process).
- CDP connects to Steam's SP tab via WebSocket and executes JS in the correct process.

### Connection

```python
# py_modules/unifideck/cdp_inject.py
class UnifideckCDPClient:
    async def connect(self):
        # GET http://127.0.0.1:8080/json → list all CEF targets
        # Find the SP tab: url contains "steamloopback.host"
        #                  title is "Steam Big Picture Mode"
        # Connect WebSocket to its webSocketDebuggerUrl
```

**Critical**: Use `/json` (page list), NOT `/json/version` (browser-level endpoint).

### Hiding Strategy

The native Play button cannot be targeted by CSS class (stale mappings). Instead:

1. **Find by text content**: Query all `button` and `[class*="Focusable"]` elements
2. **Match button text**: Regex `/^(Play|Install|Stream|Resume|Update|...)$/i`
3. **Validate by size**: `getBoundingClientRect()` — width > 100, height > 30
4. **Walk up 4 parent levels**: The play button's ancestor 4 levels up is the section container
5. **Hide with marker**: `container.style.display = 'none'` + `data-unifideck-hidden-native="{appId}"`
6. **Unhide**: Query `[data-unifideck-hidden-native="{appId}"]`, remove style + attribute

```python
# Simplified — actual code in cdp_inject.py
async def hide_native_play_section(self, appId: int) -> bool:
    js = '''
    var buttons = document.querySelectorAll('button, [class*="Focusable"]');
    // find button by text, walk up 4 parents, set display:none
    container.setAttribute("data-unifideck-hidden-native", appId);
    container.style.setProperty("display", "none", "important");
    '''
    result = await self.execute_js(js)
    return value in ("hidden", "already_hidden")
```

### Lifecycle

| Event                                | Action                                  | Called From                    |
| ------------------------------------ | --------------------------------------- | ------------------------------ |
| Patcher runs (uninstalled game)      | `hide_native_play_section(appId)`       | `index.tsx` patcher            |
| Component confirms installed         | `unhide_native_play_section(appId)`     | `PlaySectionWrapper` useEffect |
| `get_game_info` fails / returns null | `unhide_native_play_section(appId)`     | `PlaySectionWrapper` useEffect |
| User navigates to different game     | `unhide_native_play_section(prevAppId)` | `PlaySectionWrapper` useEffect |
| Plugin unloads                       | `remove_all_hide_css()`                 | `onDismount`                   |

### Race Condition Prevention

- **Operation chaining**: `pendingCDPOps` Map serializes hide/unhide for the same appId
  via `chainCDPOp()` — no concurrent CDP calls for the same game.
- **Sync cache guard**: Patcher checks `unifideckGameCache` — if game is installed,
  hide injection is skipped entirely (no blank flash).
- **Navigation cleanup**: `prevAppIdRef` tracks previous appId; on change, old hide is
  removed. Avoids unmount/remount false cleanup from React re-renders.

### Attribute Selector Quoting

CSS attribute selectors require quoted values for numeric strings:

```javascript
// ✅ Correct
document.querySelector('[data-unifideck-hidden-native="' + appId + '"]');
// ❌ Wrong — DOMException: not a valid selector
document.querySelector("[data-unifideck-hidden-native=" + appId + "]");
```

---

## 3. Key Files

| File                                          | Purpose                                                                       |
| --------------------------------------------- | ----------------------------------------------------------------------------- |
| `src/views/AppDetailsPatch.tsx`               | Route patcher, component injection into InnerContainer + marker class          |
| `src/components/play/PlaySectionWrapper.tsx`  | Custom PlaySection; renders `nativeAppDetailsHideCss()` scoped hide `<style>`   |
| `src/components/play/play.css.ts`             | `HIDE_NATIVE_PLAY_MARKER`, `nativeAppDetailsHideCss()`, focus styles            |
| `src/components/info/GameInfoPanel.tsx`       | Metadata panel (compat badge, info, synopsis, nav)                             |
| `py_modules/unifideck/cdp/cdp_inject.py`      | CDP WebSocket client (now used for xCloud streaming flows, not play hiding)    |

---

## 4. What Does NOT Work (Lessons Learned)

1. **`playSectionClasses.PlayBar`** — Resolves to `_3fLo166MlaNqP8r8tTyRz` but 0 DOM elements have this class. The `@decky/ui` class mappings are stale.
2. **CSS injection via `<style>` tags** — Works for injection, but the target selector never matches.
3. **`/json/version` CDP endpoint** — Connects to browser-level context, not SP page tab. Use `/json`.
4. **Unmount cleanup for CDP** — React re-renders trigger unmount/remount, causing premature unhide. Use `prevAppIdRef` for navigation-aware cleanup.
5. **Splicing at index 0** — Places components above the hero image. Splice at index 2+ to position below hero.

---

## 5. Steam Store page injection (CDP)

The "already owned elsewhere" ribbon is the one place Unifideck writes DOM directly, because there is no other way in. In Gaming Mode the Steam Store is a separate CEF BrowserView, composited **above** the Big Picture window (bounds measured on-device: `{x:0, y:40, w:1278, h:601}`). React rendered in Steam's own window can only paint the header and footer strips around it, which is why IsThereAnyDeal for Deck draws a fixed bar in the footer. ProtonDB Badges and DeckySales instead inject into the store page over CDP; so does this.

| Step | Where | How |
| ---- | ----- | --- |
| Notice navigation | `src/lib/steam-bridge/store-ownership-ribbon.ts` | `GamepadUIMainWindowInstance.m_StoreBrowser.StartLoadingCallbacks` and `.FinishedRequestCallbacks`, both `Register(cb)` with the URL first. No CDP. A once-a-second check re-attaches when Steam rebuilds the window or the store browser |
| Decide | `rpc/mixins/store_ownership.py` | joins the live library with `steam_real_appid`; returns `not_owned` before any CDP work |
| Draw | `cdp/store_ribbon.py` + `cdp/store_ribbon_js.py` | finds page targets on that exact AppID and evaluates the ribbon script |

Findings verified with steam-debug on 2026-10-02:

1. **The callback list is Steam's own and additive.** `FinishedRequestCallbacks` is a getter; `Register` pushes onto `m_vecCallbacks` and returns `{Unregister}`. It fired for `steam://openurl`, in-page link clicks and `GoBack()`. The browser object survived leaving the store and returning, but it is created lazily, so registration is retried on route changes into `/steamweb`.
2. **`window.MainWindowBrowserManager` is the desktop UI's browser.** In Gaming Mode it stayed on the store front page while the real store was on an app page.
3. **Back/forward loads a fresh document**, so the ribbon is redrawn on every callback and never deduplicated by URL.
4. **Only `id` anchors are stable.** The Gamepad store page is React with hashed class names; `#FeatureTarget_*` ids and `#gamepad_carousel` survive. `#game_area_purchase` exists but is `display:none` in this layout.
5. **Nothing may change height above the media carousel.** An in-flow banner inserted above it left the carousel's gamepad focus ring at its old position, because the ring's coordinates are computed when focus lands. Neither a blur/focus nor a `resize` event moved it. So the top placement is an absolute overlay inside the capsule-art container (the parent of `#gamepad_carousel img[src*="/header"]`), and the in-flow note sits below the carousel, just before `#FeatureTarget_purchase-options`.
6. **Steam rebuilds the window and the store browser after a UI restart.** Shortcut changes after a sync restart the Steam UI; a reference taken once kept pointing at the old objects and the ribbon was off for every store until a plugin reload. Hence the once-a-second check.
7. **On a page never visited before, "finished" often comes late.** Over four first visits, loading started at 0.5 to 1.1 s and the page's blocks existed at about 2 s, but `FinishedRequestCallbacks` fired at 1.4, 5.4, 7.0 and 7.1 s. So the ribbon is also requested on start-loading. At that moment the CDP target can already carry the new URL while its document is still the previous page, and the new document may not have an `<html>` element yet. The script answers `path-mismatch` in the old document and the backend retries; it observes `document` itself, not `documentElement`.
8. **Gamepad focus belongs to Valve's navigation library, and its outline is a React component.** Each `#FeatureTarget_*` block is its own React root and navigation tree. Inside it, a focus-ring root (code contains `disableFocusRing` and `OnForceMeasureFocusRing`) provides the callbacks that draw the grey outline. A node rendered without that root takes focus but shows nothing. Nothing on the page styles a `gpfocus` class.

Rules the ribbon script follows:

- Every node is built with `createElement` + `textContent`. The payload is a JSON literal (`json.dumps(..., ensure_ascii=True)`), and a test bans `innerHTML`, `insertAdjacentHTML`, `document.write` and `eval(`.
- Store logos are the same react-icons glyphs `<StoreIcon>` renders. The frontend reads them as SVG shape data (`src/lib/steam-bridge/store-icon-spec.ts`), the backend keeps only allowlisted shape tags and presentation attributes (`rpc/mixins/_store_ribbon_icons.py`), and the page rebuilds them with `createElementNS` + `setAttribute`. A logo that does not survive the allowlist falls back to a plain dot.
- It checks `location.pathname` against `/app/<appid>` **before** touching a previous instance, so an evaluation that lost a race with the next navigation does nothing.
- A window-scoped handle (`__unifideckOwnershipRibbon`) keeps a repeat call idempotent and lets a new payload replace the old one.
- A MutationObserver redraws after React re-renders; if no anchor appears within 15 s it stops and logs one console line naming the selectors.
- The capsule overlay never takes gamepad focus (`pointer-events:none`, `tabIndex=-1`). It carries the tag and a few words only, because the capsule is narrow in Gaming Mode and a sentence there was cut to "Included with Game P…": the stores for a purchase ("OWNED GOG · Xbox"), the service for a subscription ("STREAMABLE Xbox Game Pass").
- Streaming on Xbox is two different facts, kept apart. An owned game that streams because you own it gets "Cloud" on its Xbox chip (not next to "Play Anywhere", which already includes the cloud). A title in the Game Pass catalog gets its own "Xbox Game Pass" line, even when you also own it. A title that streams for a reason we cannot name gets a neutral "Xbox Cloud Gaming" line. `/v2/titles` does not say which applies, so each xCloud row carries `metadata.game_pass` from Microsoft's public Game Pass lists, matched by product id or a related bundle (`stores/microsoft/game_pass.py`). Rows synced before that flag existed read as unknown until the next sync.
- The note takes gamepad focus like a native block, because it is built like one. It is rendered with the page's own React (found in `webpackChunkstore` by source, once per document) as its own navigation tree: the page's tree component (code contains `NewGamepadNavigationTree`) with navID `unifideck-owned-nav` and the same `parentEmbeddedNavTree` as `#FeatureTarget_purchase-options`. Inside are the page's focus-ring root and `Focusable` (code contains `"flow-children"`). That component stamps `data-react-nav-root` on its div, and the legacy tree embeds every element carrying it. Only the contexts above the tree component are copied; a copied context never updates. The rows inside are still built with `createElement`/`textContent`. If any piece is missing or the render throws, the same note is drawn as plain DOM, which the D-pad skips.
- Two earlier attempts, measured with D-pad presses sent through `m_StoreBrowser.ForwardGamepadEventDetail('vgp_onbuttondown', {button: 10})` (10 = down, 9 = up):
  - Joining the purchase block's tree: down from the note jumped past the purchase options to DLC, and up from DLC came back to the note.
  - Copying that tree's context: a frozen `bActiveTree: false`, so focus landed but the outline never drew.
