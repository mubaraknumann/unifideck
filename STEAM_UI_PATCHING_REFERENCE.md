# Steam UI Patching Reference Guide

*Comprehensive reference for patching Steam Deck UI in Decky plugins*

**Sources:**
- `/home/deck/Downloads/decky-frontend-lib-main` - Decky UI library
- `/home/deck/homebrew/plugins/SDH-CssLoader/` - CSSLoader CDP implementation
- `/home/deck/Downloads/CSSLoader-Desktop-main` - CSSLoader desktop app
- `/home/deck/Downloads/moondeck-main` - MoonDeck UI patching patterns

---

## Table of Contents

1. [Decky UI Class System](#decky-ui-class-system)
2. [CDP (Chrome DevTools Protocol)](#cdp-chrome-devtools-protocol)
3. [React Tree Patching](#react-tree-patching)
4. [PlaySection Architecture](#playsection-architecture)
5. [Finding Current Class Names](#finding-current-class-names)
6. [Game Action Interception](#game-action-interception)
7. [Best Practices](#best-practices)

---

## Decky UI Class System

### Overview

Steam's UI uses **obfuscated Webpack class names** (e.g., `_2a3b4c5d`). Decky's `@decky/ui` library provides:
- Typed exports of class name mappings
- Stable references that survive Steam updates
- 1000+ exported class names across multiple modules

### Key Class Modules

```typescript
import {
  playSectionClasses,           // 117 class names - PlaySection area
  appActionButtonClasses,       // 49 class names - Play/Install buttons
  appDetailsClasses,            // App details page structure
  appDetailsHeaderClasses,      // Header-specific classes
  basicAppDetailsSectionStylerClasses,  // Section styling
  joinClassNames               // Utility for combining classes
} from "@decky/ui";
```

### PlaySectionClasses Structure

**Location in decky-frontend-lib:** `src/utils/static-classes.ts` (lines 240-358)

**Key groups (117 total classes):**

**Layout & Containers:**
- `PlayBar` - Main container
- `Container`, `InnerContainer` - Content wrappers
- `Row` - Horizontal layout
- `StatusAndStats`, `StatusNameContainer` - Info sections

**Action Buttons:**
- `ChooseButton`, `ClaimButton`, `ClaimButtonContainer`
- `ControllerConfigButton`, `FavoriteButton`
- `MenuButton`, `MenuButtonContainer`, `MenuActive`

**Progress & Status:**
- `DownloadProgressBar`, `DetailsProgressBar`
- `Downloading`, `DownloadPaused`
- `CloudStatusIcon`, `CloudStatusLabel`, `CloudStatusRow`

**Game Stats:**
- `GameStat`, `GameStatIcon`, `GameStatRight`
- `LastPlayed`, `LastPlayedInfo`
- `Playtime`, `PlaytimeIcon`

**UI States:**
- `Visible`, `FeatureHidden`, `Disabled`
- `InvalidPlatform`, `OfflineMode`
- `SharedLibrary`, `ComingSoon`

**Responsive Breakpoints:**
- `BreakNarrow`, `BreakShort`, `BreakTall`, `BreakWide`, `BreakUltraWide`
- `GamepadUIBreakNarrow`, `GamepadUIBreakWide`

**Animation:**
- `BackgroundAnimation`, `focusAnimation`, `hoverAnimation`
- `SyncAnim`, `duration-app-launch`
- `ItemFocusAnim-*` (multiple color variants)

### AppActionButtonClasses Structure

**Location in decky-frontend-lib:** `src/utils/static-classes.ts` (lines 902-951)

**Key classes (49 total):**
- `PlayButton`, `PlayButtonContainer` - Main action button
- `Green` - Green button variant (for Play)
- `Disabled`, `NoAction` - Button states
- `ShutdownAppButton`, `ForceShutdownButton` - App control
- `Throbber`, `ThrobberContainer` - Loading states
- Same responsive/animation classes as PlaySection

### How Class Names Work

**Webpack Module Pattern:**
```typescript
// Internal module structure (not directly accessible):
{
  "PlayBar": "_2a3b4c5d",
  "PlayBarDetailLabel": "_3e4f5g6h",
  "Container": "_4g5h6i7j",
  // ... 117 more
}

// Decky exports:
export const playSectionClasses = findClassModule(
  (m) => m.PlayBarDetailLabel  // Distinctive class used as finder
) as PlaySectionClasses;
```

**Usage in code:**
```typescript
// Get obfuscated class name:
const className = playSectionClasses.PlayBar;  // Returns "_2a3b4c5d" (actual value varies)

// Use in JSX:
<div className={playSectionClasses.PlayBar}>Content</div>

// Combine multiple classes:
<div className={joinClassNames(
  playSectionClasses.MenuButton,
  "custom-class"
)}>
```

---

## CDP (Chrome DevTools Protocol)

### Overview

CDP enables **cross-process CSS/JS injection** in Steam's Chromium Embedded Framework (CEF). Required because:
- Decky plugins run in isolated CEF process
- Direct DOM manipulation doesn't work across processes
- Must use CDP to reach Steam's UI process

### Architecture

**CSSLoader Implementation:** `/home/deck/homebrew/plugins/SDH-CssLoader/css_browserhook.py`

```
Plugin Process                    Steam UI Process
     |                                   |
     | 1. GET /json/version              |
     |---------------------------------->|
     |    Returns webSocketDebuggerUrl   |
     |<----------------------------------|
     |                                   |
     | 2. WebSocket connect              |
     |=================================>|
     |                                   |
     | 3. Target.setDiscoverTargets      |
     |---------------------------------->|
     |    Returns list of tabs           |
     |<----------------------------------|
     |                                   |
     | 4. Target.attachToTarget          |
     |---------------------------------->|
     |    Returns sessionId              |
     |<----------------------------------|
     |                                   |
     | 5. Runtime.evaluate (with sessionId)
     |---------------------------------->|
     |    Executes JS in tab             |
     |    Creates <style> elements       |
     |<----------------------------------|
```

### CEF Remote Debugging Setup

**Required file:** `~/.steam/steam/.cef-enable-remote-debugging` (empty file)

```python
def create_cef_debugging_flag():
    steam_path = get_steam_path()  # ~/.steam/steam or ~/.local/share/Steam
    flag_path = os.path.join(steam_path, ".cef-enable-remote-debugging")

    if not os.path.exists(flag_path):
        with open(flag_path, 'w') as f:
            pass  # Empty file
        return True  # Steam restart required
    return False
```

**After creation:** Must restart Steam for CEF to enable debugging port.

### CDP Connection Flow

```python
import aiohttp
import asyncio

class UnifideckCDPClient:
    def __init__(self):
        self.websocket = None
        self.client = None
        self.ws_url = None
        self.msg_id = 0
        self.connected = False

    async def connect(self):
        # Step 1: Get CDP endpoint
        async with aiohttp.ClientSession() as web:
            res = await web.get(
                "http://127.0.0.1:8080/json/version",
                timeout=aiohttp.ClientTimeout(total=3)
            )
            data = await res.json()
            self.ws_url = data["webSocketDebuggerUrl"]

        # Step 2: Connect WebSocket
        self.client = aiohttp.ClientSession()
        self.websocket = await self.client.ws_connect(self.ws_url)
        self.connected = True

    async def execute_js(self, js: str) -> dict:
        self.msg_id += 1

        await self.websocket.send_json({
            "id": self.msg_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": js,
                "userGesture": True,
                "awaitPromise": False,
                "returnByValue": True
            }
        })

        # Wait for response with matching ID
        start_time = asyncio.get_event_loop().time()
        async for msg in self.websocket:
            data = msg.json()
            if data.get("id") == self.msg_id:
                return data

            if asyncio.get_event_loop().time() - start_time > 5:
                raise Exception("CDP timeout")

        raise Exception("CDP connection closed")
```

### CSS Injection Pattern

```python
async def inject_hide_css(self, appId: int) -> str:
    css_id = f"unifideck-hide-native-play-{appId}"

    css_rules = """
._3Yf8b2v5oOD8Wqsxu04ar:not([data-unifideck-play-wrapper]) { display: none !important; }
._2L3s2nzh7yCnNESfI5_dN1:not([data-unifideck-play-wrapper]) { display: none !important; }
._3scbHORkYB7utTUGfkMCC_:not([data-unifideck-play-wrapper]) { display: none !important; }
    """

    js = f"""
(function() {{
    let styleId = '{css_id}';
    if (document.getElementById(styleId)) return styleId;

    let style = document.createElement('style');
    style.id = styleId;
    style.textContent = `{css_rules}`;
    document.head.appendChild(style);

    return styleId;
}})()
    """

    result = await self.execute_js(js)
    return css_id
```

### Reconnection & Keep-Alive

**CSSLoader Pattern (3-second health checks):**

```python
async def health_check(self):
    while True:
        await asyncio.sleep(3)
        try:
            res = await web.get("http://127.0.0.1:8080/json/version", timeout=3)
            if res.status != 200:
                raise Exception("CDP endpoint unavailable")

            data = await res.json()
            self.ws_url = data["webSocketDebuggerUrl"]
            await self.open_websocket()
        except Exception as e:
            # Connection lost, will retry in 3 seconds
            await self.disconnect()
```

**Unifideck Pattern (reconnect on error):**

```python
async def inject_hide_css_cdp(self, appId: int):
    try:
        client = await get_cdp_client()
        css_id = await client.inject_hide_css(appId)
        return {"success": True, "css_id": css_id}
    except Exception as e:
        # Reconnect once on transport errors
        if "transport" in str(e).lower() or "closing" in str(e).lower():
            await shutdown_cdp_client()
            client = await get_cdp_client()
            css_id = await client.inject_hide_css(appId)
            return {"success": True, "css_id": css_id}
        return {"success": False, "error": str(e)}
```

### CDP Protocol Commands

**Key commands used by CSSLoader:**

- `Target.setDiscoverTargets` - Enable tab discovery
- `Target.getTargets` - List all browser targets/tabs
- `Target.attachToTarget` - Attach to tab (returns sessionId)
- `Target.targetCreated` - Event: New tab opened
- `Target.targetInfoChanged` - Event: Tab URL/title changed
- `Target.detachedFromTarget` - Event: Tab closed
- `Runtime.evaluate` - Execute JavaScript in page

**SessionID usage:**
- Each tab has unique sessionId
- Required for tab-specific commands
- Obtained from `Target.attachedToTarget` response

---

## React Tree Patching

### Overview

**CEF Process Isolation Requirements:**
- ❌ Direct DOM manipulation doesn't work
- ❌ `document.querySelector()` finds wrong process
- ✅ Must use React tree patching via `routerHook.addPatch`

**Pattern:** Intercept React's render, mutate element tree before reconciliation

### Core API

**From `@decky/ui`:**

```typescript
import {
  createReactTreePatcher,  // Main patching API
  findInReactTree,        // Tree traversal
  afterPatch,             // Function wrapper
  routerHook              // Router patching
} from "@decky/ui";
```

### Step-by-Step Pattern

**1. Find the Route to Patch**

```typescript
// In your plugin's main file:
routerHook.addPatch("/library/app/:appid", (routeProps: any) => {
  // routeProps contains the route's render function

  afterPatch(routeProps, "renderFunc", (args: any[], ret: ReactElement) => {
    // 'ret' is the rendered React element tree
    // Mutate it here before React reconciles
    return ret;
  });

  return routeProps;
});
```

**2. Find Elements in the Tree**

```typescript
afterPatch(routeProps, "renderFunc", (args, ret) => {
  // Find by class name
  const container = findInReactTree(ret, (x: any) =>
    x?.props?.className?.includes(appDetailsClasses.InnerContainer)
  );

  // Find by prop structure
  const overview = findInReactTree(ret, (x: any) =>
    x?.props?.children?.props?.overview
  );

  // Find by component type
  const playSection = findInReactTree(ret, (x: any) =>
    Array.isArray(x?.props?.children) &&
    x?.type?.toString().includes("PlaySection")
  );

  return ret;
});
```

**3. Mutate the Element Tree**

```typescript
afterPatch(routeProps, "renderFunc", (args, ret) => {
  const parent = findInReactTree(ret, (x: any) =>
    x?.props?.className?.includes(appDetailsClasses.InnerContainer)
  );

  if (!parent || !Array.isArray(parent.props.children)) {
    return ret;
  }

  // Insert at specific index
  parent.props.children.splice(2, 0,
    React.createElement(MyCustomComponent, { appId })
  );

  // Or replace element
  const playIndex = parent.props.children.findIndex(
    x => x?.props?.id === "play-section"
  );
  if (playIndex >= 0) {
    parent.props.children[playIndex] =
      React.createElement(MyPlayButton, { appId });
  }

  return ret;
});
```

### MoonDeck Two-Phase Pattern

**Extraction + Mutation for complex patching:**

```typescript
const patchHandler = createReactTreePatcher(
  [
    // Phase 1: Extract data
    (tree: any) => {
      const children = findInReactTree(
        tree,
        (x: any) => x?.props?.children?.props?.overview
      )?.props?.children;

      const overview = children.props?.overview;
      const appId = overview?.appid;
      const displayName = overview?.display_name;

      return children;  // Return node to patch
    }
  ],
  // Phase 2: Mutate
  (nodes: Array<any>, ret?: ReactElement) => {
    const parent = findInReactTree(ret, (x: any) =>
      Array.isArray(x?.props?.children) &&
      x?.props?.className?.includes(appDetailsClasses.InnerContainer)
    );

    // Smart insertion: find position relative to other elements
    const hltbIndex = parent.props.children.findIndex(
      x => x.props.id === "hltb-for-deck"
    );
    const appPanelIndex = parent.props.children.findIndex(
      x => x.props.overview
    );

    const insertIndex = hltbIndex >= 0
      ? hltbIndex
      : (appPanelIndex >= 0 ? appPanelIndex - 1 : -1);

    parent.props.children.splice(insertIndex, 0,
      <MyButton appId={appId} name={displayName} />
    );

    return ret;
  }
);

// Apply the patcher
afterPatch(routeProps, "renderFunc", patchHandler);
```

### Finding Complex Elements

**MoonDeck Pattern for TopCapsule:**

```typescript
function findTopCapsuleParent(ref: HTMLDivElement | null): Element | null {
  const children = ref?.parentElement?.children;

  for (const child of children) {
    if (child.className.includes(appDetailsClasses.Header)) {
      const headerContainer = child;

      for (const child of headerContainer.children) {
        if (child.className.includes(appDetailsHeaderClasses.TopCapsule)) {
          return child;
        }
      }
    }
  }

  return null;
}
```

### Watching for State Changes

**MutationObserver Pattern:**

```typescript
useEffect(() => {
  const topCapsule = findTopCapsuleParent(anchorRef.current);
  if (!topCapsule) return;

  const observer = new MutationObserver((entries) => {
    for (const entry of entries) {
      const className = (entry.target as Element).className;

      const isFullscreen =
        className.includes(appDetailsHeaderClasses.FullscreenEnterStart) ||
        className.includes(appDetailsHeaderClasses.FullscreenEnterActive);

      setShow(!isFullscreen);
    }
  });

  observer.observe(topCapsule, {
    attributes: true,
    attributeFilter: ["class"]
  });

  return () => observer.disconnect();
}, []);
```

---

## PlaySection Architecture

### PlaySection Structure

**Location:** Rendered in `/library/app/:appid` route

**React Tree Hierarchy:**
```
AppDetailsPage
  └─ AppDetailsLayout
      └─ InnerContainer (appDetailsClasses.InnerContainer)
          ├─ [Index 0] HeaderCapsule
          ├─ [Index 1] GameInfoPanel (custom - Unifideck adds this)
          ├─ [Index 2] PlaySection ← TARGET AREA
          │   ├─ Container (playSectionClasses.Container)
          │   │   ├─ InnerContainer (playSectionClasses.InnerContainer)
          │   │   │   ├─ PlayButtonContainer (appActionButtonClasses.PlayButtonContainer)
          │   │   │   │   └─ PlayButton (appActionButtonClasses.PlayButton + .Green)
          │   │   │   ├─ StatusAndStats (playSectionClasses.StatusAndStats)
          │   │   │   │   ├─ LastPlayed (playSectionClasses.LastPlayed)
          │   │   │   │   └─ Playtime (playSectionClasses.Playtime)
          │   │   │   └─ RightControls (playSectionClasses.RightControls)
          │   │   │       ├─ MenuButton (controller config)
          │   │   │       └─ MenuButton (settings)
          ├─ [Index 3] AboutThisGame
          └─ [Index 4+] More sections...
```

### Unifideck PlaySection Strategy

**Files:**
- `src/components/PlayButtonOverride.tsx` - Custom PlaySection component
- `src/index.tsx` - Patcher that injects component

**Strategy:**
1. **Inject custom component at index 0** (before PlaySection)
2. **Hide native PlaySection via CDP** when game is uninstalled
3. **Show native PlaySection** when game is installed (remove CDP CSS)

**Patcher (index.tsx):**

```typescript
routerHook.addPatch("/library/app/:appid", (routeProps: any) => {
  afterPatch(routeProps, "renderFunc", (args, ret) => {
    const appId = parseInt(args[0].match.params.appid);

    // Find InnerContainer
    const container = findInReactTree(ret, (x: any) =>
      Array.isArray(x?.props?.children) &&
      x?.props?.className?.includes(appDetailsClasses.InnerContainer)
    );

    if (!container) return ret;

    // Check if already injected
    const alreadyHasWrapper = container.props.children.some(
      (x: any) => x?.props?.['data-unifideck-play-wrapper'] === "true"
    );

    if (!alreadyHasWrapper) {
      // Inject hide CSS via CDP (async, non-blocking)
      injectHidePlaySectionCDP(appId);

      // Inject wrapper at index 0 (synchronous)
      container.props.children.splice(0, 0,
        React.createElement(PlaySectionWrapper, {
          appId,
          key: `unifideck-play-${appId}`
        })
      );
    }

    return ret;
  });

  return routeProps;
});
```

**Component (PlayButtonOverride.tsx):**

```typescript
export const PlaySectionWrapper: FC<{ appId: number }> = ({ appId }) => {
  const [gameInfo, setGameInfo] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [downloadState, setDownloadState] = useState({
    isDownloading: false,
    progress: 0
  });

  // Determine if we should show custom UI
  const shouldShowCustom =
    !loading &&
    gameInfo &&
    !gameInfo.error &&
    (!gameInfo.is_installed || downloadState.isDownloading);

  // Remove hide CSS when game is installed
  useEffect(() => {
    if (!loading &&
        gameInfo?.is_installed &&
        !downloadState.isDownloading) {
      removeHidePlaySectionCDP(appId);
    }
  }, [loading, gameInfo, downloadState.isDownloading, appId]);

  // Render hidden anchor if not showing custom UI
  if (!shouldShowCustom) {
    return (
      <div
        data-unifideck-play-wrapper="true"
        style={{ display: "none" }}
      />
    );
  }

  // Render custom PlaySection
  return (
    <div
      data-unifideck-play-wrapper="true"
      style={{
        display: "flex",
        alignItems: "center",
        padding: "16px 16px 16px 0",
        gap: "0"
      }}
    >
      <Focusable className={appActionButtonClasses.PlayButtonContainer}>
        <DialogButton
          className={joinClassNames(
            appActionButtonClasses.PlayButton,
            !downloadState.isDownloading && appActionButtonClasses.Green
          )}
          onClick={handleClick}
        >
          {downloadState.isDownloading
            ? `Cancel (${downloadState.progress}%)`
            : "Install"}
        </DialogButton>
      </Focusable>

      <div style={{ display: "flex", gap: "24px", marginLeft: "24px" }}>
        <div>
          <div style={{ fontSize: "10px", textTransform: "uppercase", color: "#acb2b8" }}>
            LAST PLAYED
          </div>
          <div style={{ color: "#c7d5e0", fontSize: "14px" }}>
            {formatLastPlayed(lastPlayedTimestamp)}
          </div>
        </div>
        <div>
          <div style={{ fontSize: "10px", textTransform: "uppercase", color: "#acb2b8" }}>
            PLAY TIME
          </div>
          <div style={{ color: "#c7d5e0", fontSize: "14px" }}>
            {formatPlaytime(playtimeMinutes)}
          </div>
        </div>
      </div>
    </div>
  );
};
```

### CSS Hiding Strategy

**Via CDP (cross-process):**

```typescript
// Frontend calls backend
export async function injectHidePlaySectionCDP(appId: number) {
  const result = await call<[number], { success: boolean }>(
    "inject_hide_css_cdp",
    appId
  );
}

// Backend injects CSS
async def inject_hide_css_cdp(self, appId: int):
    client = await get_cdp_client()
    css_id = await client.inject_hide_css(appId)
    return {"success": True, "css_id": css_id}
```

**CSS selectors target native PlaySection classes:**

```css
._3Yf8b2v5oOD8Wqsxu04ar:not([data-unifideck-play-wrapper]) { display: none !important; }
._2L3s2nzh7yCnNESfI5_dN1:not([data-unifideck-play-wrapper]) { display: none !important; }
._3scbHORkYB7utTUGfkMCC_:not([data-unifideck-play-wrapper]) { display: none !important; }
```

**`:not([data-unifideck-play-wrapper])` ensures:**
- Only hides native PlaySection
- Doesn't hide our custom component (which has the attribute)

---

## Finding Current Class Names

### Problem: Steam Updates Change Obfuscated Names

Steam occasionally updates Webpack, changing class hashes like:
- `_3Yf8b2v5oOD8Wqsxu04ar` → `_4Zg9c3w6pPE9XrtywV05bs`

### Solution 1: Use Decky's Exports (Preferred)

**Best approach:** Always use `@decky/ui` exports

```typescript
import { playSectionClasses, appActionButtonClasses } from "@decky/ui";

// These are automatically updated by Decky maintainers
const className = playSectionClasses.PlayBar;
```

**Why this works:**
- Decky scans webpack modules on each load
- Uses finder functions to locate modules
- Updates automatically when Steam updates

### Solution 2: Inspect DOM in DevTools

**Manual discovery steps:**

1. **Open Steam DevTools:**
   - Ctrl+Shift+I in Gaming Mode
   - Or: Settings → Developer → Enable CEF Remote Debugging

2. **Navigate to game page:**
   - Go to `/library/app/:appid`

3. **Inspect PlaySection:**
   - Right-click Play button → Inspect
   - Look at parent hierarchy

4. **Example DOM structure:**
```html
<div class="_3Yf8b2v5oOD8Wqsxu04ar"> ← PlaySection Container
  <div class="_2L3s2nzh7yCnNESfI5_dN1"> ← InnerContainer
    <div class="_3scbHORkYB7utTUGfkMCC_"> ← PlayButtonContainer
      <button class="_1bB2a3c4d5e6f7g8h9i0 _9i8h7g6f5e4d3c2b1a0">
        Play
      </button>
    </div>
  </div>
</div>
```

5. **Update CSS selectors:**
```typescript
const css_rules = `
._3Yf8b2v5oOD8Wqsxu04ar:not([data-unifideck-play-wrapper]) { display: none !important; }
._2L3s2nzh7yCnNESfI5_dN1:not([data-unifideck-play-wrapper]) { display: none !important; }
._3scbHORkYB7utTUGfkMCC_:not([data-unifideck-play-wrapper]) { display: none !important; }
`;
```

### Solution 3: Use Decky's Class Mapper

**Access webpack modules directly:**

```typescript
import { findClassModule, classMap } from "@decky/ui";

// Find by distinctive property
const myModule = findClassModule((m) =>
  m.PlayBarDetailLabel  // Known unique property
);

console.log(myModule);
// { PlayBar: "_3Yf8b2v5oOD8Wqsxu04ar", ... }

// Or search all modules
for (const module of classMap) {
  if (module.PlayButton) {
    console.log("Found PlayButton:", module.PlayButton);
  }
}
```

### Solution 4: Runtime Detection (Advanced)

**Discover classes at runtime:**

```typescript
function findPlaySectionElement(): Element | null {
  // Use known structure
  const allDivs = document.querySelectorAll('[class]');

  for (const div of allDivs) {
    const classes = Array.from(div.classList);

    // Look for distinctive patterns
    if (classes.some(c => c.startsWith('_')) &&
        div.querySelector('[class*="PlayButton"]')) {
      return div;
    }
  }

  return null;
}

// Extract class names
const playSection = findPlaySectionElement();
if (playSection) {
  const classNames = Array.from(playSection.classList);
  console.log("PlaySection classes:", classNames);
}
```

### Checking if Classes Are Current

**Test in browser console:**

```javascript
// Check if element exists with class
document.querySelector('._3Yf8b2v5oOD8Wqsxu04ar');
// null = class is outdated
// element = class is current

// Check all potential classes
['_3Yf8b2v5oOD8Wqsxu04ar', '_2L3s2nzh7yCnNESfI5_dN1', '_3scbHORkYB7utTUGfkMCC_']
  .map(c => ({ class: c, found: !!document.querySelector('.' + c) }));
```

---

## Game Action Interception

### Overview

Intercept Steam's game launch flow to show install modal before launch.

**Pattern:** Register callback before Steam processes action, cancel if needed.

### Implementation

**MoonDeck Pattern (most reliable):**

```typescript
// src/hooks/gameActionInterceptor.ts
import { useEffect } from "react";

export function useGameActionInterceptor(
  callback: (appId: string, cancel: () => void) => void
) {
  useEffect(() => {
    const unregister = window.SteamClient?.Apps?.RegisterForGameActionStart(
      (gameActionId: number, appIdStr: string, action: string) => {
        if (action === "LaunchApp") {
          callback(appIdStr, () => {
            window.SteamClient?.Apps?.CancelGameAction(gameActionId);
          });
        }
      }
    );

    return () => unregister?.unregister();
  }, [callback]);
}
```

**Usage in component:**

```typescript
useGameActionInterceptor((appId, cancel) => {
  const gameInfo = getGameInfo(appId);

  if (gameInfo?.store === "epic" && !gameInfo.is_installed) {
    cancel();  // Prevent launch

    showModal(
      <InstallModal
        gameInfo={gameInfo}
        onInstall={() => startInstall(gameInfo)}
      />
    );
  }
});
```

### Triggering Game Actions

**From custom button:**

```typescript
const handleClick = () => {
  // Trigger Steam's game action flow
  // This will be caught by the interceptor
  window.SteamClient?.Apps?.RunGame(
    appId.toString(),
    "",    // launchOptions
    -1,    // unknown
    100    // unknown
  );
};
```

### Steam Types

**Add to `src/types/steam.ts`:**

```typescript
declare global {
  interface Window {
    SteamClient: {
      Apps: {
        RegisterForGameActionStart: (
          callback: (
            gameActionId: number,
            appId: string,
            action: string,
            unknown: any
          ) => void
        ) => { unregister: () => void };

        CancelGameAction: (gameActionId: number) => void;

        RunGame: (
          appId: string,
          launchOptions: string,
          unknown1: number,
          unknown2: number
        ) => void;

        GetAppOverview: (appId: number) => {
          rt_last_time_played?: number;
          minutes_playtime_forever?: string;
        };

        ShowControllerConfigurator?: (appId: number) => void;
        OpenAppSettingsDialog?: (appId: number, panel: string) => void;
      };
    };
  }
}

export {};
```

---

## Best Practices

### 1. Always Use React Tree Patching

**❌ Don't:**
```typescript
// Direct DOM manipulation (doesn't work in CEF)
document.querySelector('.play-button').style.display = 'none';
```

**✅ Do:**
```typescript
// React tree patching
routerHook.addPatch("/library/app/:appid", (routeProps) => {
  afterPatch(routeProps, "renderFunc", (args, ret) => {
    const container = findInReactTree(ret, ...);
    container.props.children.splice(0, 0, <MyComponent />);
    return ret;
  });
  return routeProps;
});
```

### 2. Use Decky UI Class Exports

**❌ Don't:**
```typescript
// Hardcoded obfuscated classes (breaks on Steam updates)
const className = "_3Yf8b2v5oOD8Wqsxu04ar";
```

**✅ Do:**
```typescript
// Decky-managed exports (updates automatically)
import { playSectionClasses } from "@decky/ui";
const className = playSectionClasses.PlayBar;
```

### 3. Handle Async CDP Gracefully

**❌ Don't:**
```typescript
// Blocking on CDP
await injectHidePlaySectionCDP(appId);
container.props.children.splice(0, 0, <Component />);
```

**✅ Do:**
```typescript
// Non-blocking CDP + sync React patching
injectHidePlaySectionCDP(appId);  // Async, non-blocking
container.props.children.splice(0, 0, <Component />);  // Synchronous
```

### 4. Use Data Attributes for Deduplication

**❌ Don't:**
```typescript
// No way to check if already injected
container.props.children.push(<MyComponent />);
```

**✅ Do:**
```typescript
// Check for data attribute
const alreadyHasWrapper = container.props.children.some(
  x => x?.props?.['data-unifideck-play-wrapper'] === "true"
);

if (!alreadyHasWrapper) {
  container.props.children.splice(0, 0,
    <MyComponent data-unifideck-play-wrapper="true" />
  );
}
```

### 5. Clean Up Resources on Dismount

**❌ Don't:**
```typescript
// No cleanup
routeManager.init();
```

**✅ Do:**
```typescript
// In Plugin class
export default class Plugin {
  routePatches: Array<() => void> = [];

  async _main() {
    this.routePatches.push(
      routerHook.addPatch("/library/app/:appid", ...)
    );
  }

  async _unload() {
    // Always clean up first
    this.routePatches.forEach(unpatch => unpatch());

    // Then other cleanup
    await shutdown_cdp_client();
  }
}
```

### 6. Index Placement for Compatibility

**❌ Don't:**
```typescript
// Index 0 conflicts with ProtonDB
container.props.children.splice(0, 0, <MyComponent />);
```

**✅ Do (if multiple plugins):**
```typescript
// Index 2 avoids conflicts (ProtonDB uses 0-1)
container.props.children.splice(2, 0, <MyComponent />);
```

### 7. Use :not() Selectors for CDP CSS

**❌ Don't:**
```css
/* Hides everything, including custom components */
._3Yf8b2v5oOD8Wqsxu04ar { display: none !important; }
```

**✅ Do:**
```css
/* Only hides elements without our data attribute */
._3Yf8b2v5oOD8Wqsxu04ar:not([data-unifideck-play-wrapper]) {
  display: none !important;
}
```

### 8. Reconnect CDP on Errors

**❌ Don't:**
```python
# No reconnection logic
async def inject_hide_css_cdp(self, appId):
    client = await get_cdp_client()
    return await client.inject_hide_css(appId)
```

**✅ Do:**
```python
# Reconnect on transport errors
async def inject_hide_css_cdp(self, appId):
    try:
        client = await get_cdp_client()
        return await client.inject_hide_css(appId)
    except Exception as e:
        if "transport" in str(e).lower():
            await shutdown_cdp_client()
            client = await get_cdp_client()
            return await client.inject_hide_css(appId)
        raise
```

---

## Quick Reference

### Key Imports

```typescript
import {
  playSectionClasses,
  appActionButtonClasses,
  appDetailsClasses,
  createReactTreePatcher,
  findInReactTree,
  afterPatch,
  routerHook,
  DialogButton,
  Focusable,
  showModal,
  ConfirmModal
} from "@decky/ui";

import { call, toaster } from "@decky/api";
```

### Common Patterns

**Patch game details page:**
```typescript
routerHook.addPatch("/library/app/:appid", (routeProps) => { ... });
```

**Find InnerContainer:**
```typescript
const container = findInReactTree(ret, (x) =>
  x?.props?.className?.includes(appDetailsClasses.InnerContainer)
);
```

**Insert component:**
```typescript
container.props.children.splice(index, 0, <Component />);
```

**Inject CDP CSS:**
```typescript
injectHidePlaySectionCDP(appId);  // Frontend
await client.inject_hide_css(appId)  # Backend
```

**Intercept game launch:**
```typescript
window.SteamClient.Apps.RegisterForGameActionStart((id, appId, action) => {
  if (action === "LaunchApp") {
    // Handle or cancel
  }
});
```

---

## Troubleshooting

### "Cannot write to closing transport"
- **Cause:** CDP websocket closed/timeout
- **Fix:** Add reconnection logic with `shutdown_cdp_client()` + `get_cdp_client()`

### Native PlaySection Not Hidden
- **Cause:** CSS class names outdated
- **Fix:** Inspect DOM, update selectors in `cdp_inject.py`

### Custom Component Not Appearing
- **Cause:** Not using React tree patching
- **Fix:** Use `routerHook.addPatch` + `afterPatch`, not DOM manipulation

### Changes Don't Persist After Navigation
- **Cause:** React re-renders without re-patching
- **Fix:** Ensure patch is registered on route, not on mount

### Multiple Components Injected
- **Cause:** No deduplication check
- **Fix:** Use data attribute to check if already injected

---

## References

- **Decky Frontend Lib:** `/home/deck/Downloads/decky-frontend-lib-main`
- **CSSLoader Plugin:** `/home/deck/homebrew/plugins/SDH-CssLoader/`
- **MoonDeck Plugin:** `/home/deck/Downloads/moondeck-main`
- **Decky Docs:** https://docs.deckbrew.xyz/
- **Steam CDP Port:** http://127.0.0.1:8080/json/version

---

*Last updated: 2026-02-09*
