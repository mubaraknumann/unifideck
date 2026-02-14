# UI Injection Guide — Unifideck (Steam Deck / Decky Loader)

This document describes the **working** UI injection methods used by Unifideck as of 2026-02.

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

| Component          | Positioned After       | Strategy                                                         |
| ------------------ | ---------------------- | ---------------------------------------------------------------- |
| PlaySectionWrapper | Native PlaySection     | `findPlaySectionInsertIndex()` — finds native PlaySection child  |
| GameInfoPanel      | PlaySectionWrapper     | `findIndex()` on PlaySectionWrapper key, insert after            |
| InstallInfoDisplay | GameInfoPanel          | `findIndex()` on GameInfoPanel key, insert after                 |

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

## 2. CDP Native Play Button Hiding (DOM Manipulation)

Used for: **Hiding Steam's native Play/Install button** for uninstalled Unifideck games.

### Why CDP?

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

| File                                    | Purpose                                                                |
| --------------------------------------- | ---------------------------------------------------------------------- |
| `src/index.tsx`                         | Route patcher, component injection into InnerContainer                 |
| `src/components/PlayButtonOverride.tsx` | PlaySectionWrapper + CDP hide/unhide wrappers                          |
| `src/components/GameInfoPanel.tsx`      | Metadata panel (compat badge, info, synopsis, nav)                     |
| `py_modules/unifideck/cdp_inject.py`    | CDP WebSocket client, JS execution, hide/unhide                        |
| `main.py`                               | Backend RPC (`hide_native_play_section`, `unhide_native_play_section`) |

---

## 4. What Does NOT Work (Lessons Learned)

1. **`playSectionClasses.PlayBar`** — Resolves to `_3fLo166MlaNqP8r8tTyRz` but 0 DOM elements have this class. The `@decky/ui` class mappings are stale.
2. **CSS injection via `<style>` tags** — Works for injection, but the target selector never matches.
3. **`/json/version` CDP endpoint** — Connects to browser-level context, not SP page tab. Use `/json`.
4. **Unmount cleanup for CDP** — React re-renders trigger unmount/remount, causing premature unhide. Use `prevAppIdRef` for navigation-aware cleanup.
5. **Splicing at index 0** — Places components above the hero image. Splice at index 2+ to position below hero.
