# GameInfoPanel Container Structure - Reference Guide

This document consolidates findings from exploring the Steam Deck game details page container structure for positioning the GameInfoPanel component.

## Problem Statement

GameInfoPanel needs to appear:
- BELOW the play button row
- ABOVE the tabs (ACTIVITY, YOUR STUFF, COMMUNITY, GAME INFO)
- Without using fragile absolute positioning with hardcoded pixel values

## Container Hierarchy

The Steam Deck game details page uses the following structure:

```
AppDetailsInnerContainer (flexbox: column)
├─ [0] PlaySection
│   ├─ Play button
│   ├─ Install button
│   └─ Action buttons (gear icon, etc.)
│
├─ [1] GamepadTabbedPage
│   ├─ Tab headers (ACTIVITY, YOUR STUFF, COMMUNITY, GAME INFO)
│   └─ Tab content (dynamically rendered based on active tab)
│
└─ [INJECTED COMPONENTS]
    ├─ InstallInfoDisplay (position: absolute, top-right)
    ├─ ProtonMedal (index 1, ProtonDB plugin)
    └─ GameInfoPanel (our component)
```

## Key Static Classes (from decky-frontend-lib)

### appDetailsClasses
```typescript
{
  Container: string,           // Root app details container
  InnerContainer: string,      // Main scrollable area (WHERE COMPONENTS ARE INJECTED)
  Header: string,              // Fixed header area
  ScrollContainer: string,     // Handles scrolling
  PlayBar: string,            // Play button bar
}
```

### playSectionClasses
```typescript
{
  Container: string,           // Play section root
  PlayBar: string,            // Play button bar
  AppButtonsContainer: string, // Action buttons (Install, Play, etc.)
}
```

### gamepadTabbedPageClasses
```typescript
{
  GamepadTabbedPage: string,   // Root tab container (class: _3IBLc81yyL08OJ7rfKtF00)
  TabRow: string,              // Tab header row
  TabContents: string,         // Active tab content
  TabContentsScroll: string,   // Scrollable content
}
```

## Component Injection Patterns

### Pattern 1: ProtonDB Medal (Proven Pattern)
```typescript
// File: protondb-decky/src/lib/patchLibraryApp.tsx
const container = findInReactTree(
  ret,
  (x) =>
    Array.isArray(x?.props?.children) &&
    x?.props?.className?.includes(appDetailsClasses.InnerContainer)
);

container.props.children.splice(1, 0, <ProtonMedal />);
```

**Why it works:**
- Inserts at index 1 (between PlaySection and tabs)
- Flex container handles positioning naturally
- No magic pixel values
- Visual order matches DOM order

### Pattern 2: InstallInfoDisplay (Absolute Positioning)
```typescript
// File: unifideck-decky/src/index.tsx
<div
  style={{
    position: "absolute",
    top: "40px",      // Fixed position in corner
    right: "35px",
    zIndex: 9999,
  }}
>
  {/* Install button */}
</div>
```

**Why it works:**
- Corner positioning is stable regardless of content height
- High z-index ensures visibility
- Doesn't affect flex layout

**When to use:**
- Small UI elements (buttons, badges)
- Corner positioning
- Must appear regardless of scroll position

**When NOT to use:**
- Large content panels
- Position depends on other elements
- Needs to scroll with page

## Recommended Solution: ProtonDB Pattern

### Implementation

**index.tsx:**
```typescript
const isNonSteamGame = appId > 2000000000;
if (isNonSteamGame && !alreadyHasGameInfo) {
  // Insert at index 1: between PlaySection [0] and Tabs [1+]
  container.props.children.splice(
    1,
    0,
    React.createElement(GameInfoPanel, {
      key: gameInfoKey,
      appId,
    })
  );
}
```

**GameInfoPanel.tsx:**
```typescript
// Normal document flow - no absolute positioning needed
const containerStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "16px",
  padding: "16px",
  backgroundColor: "rgba(0, 0, 0, 0.2)",
  borderRadius: "8px",
};

return (
  <div style={containerStyle}>
    {/* Content */}
  </div>
);
```

## Why This Solution Works

✅ **Proven Pattern**: ProtonDB uses this across thousands of users
✅ **Responsive**: Adapts to all screen sizes automatically
✅ **No Magic Numbers**: Flex layout determines position, not hardcoded pixels
✅ **DOM-Order Matching**: Visual order matches code order (better accessibility)
✅ **Maintainable**: Self-documenting, easy to understand
✅ **Low Risk**: Copying battle-tested pattern from proven plugins

## Common Pitfalls

### ❌ Using push() or high index values
```typescript
// BAD: Adds to end, appears at bottom due to tabs' flex-grow: 1
container.props.children.push(gameInfoPanel);

// BAD: Same result - tabs fill remaining space
container.props.children.splice(5, 0, gameInfoPanel);
```

### ❌ Hardcoded absolute positioning
```typescript
// FRAGILE: Breaks on different screen sizes/layouts
position: "absolute",
top: "320px",  // Magic number that varies per game/screen
```

### ❌ Complex tree searching
```typescript
// UNRELIABLE: Structure may vary between games
const tabbedPageParent = findInReactTree(/* complex logic */);
```

## React Tree Patching Gotchas

### Container Selection
Always target `appDetailsClasses.InnerContainer` for game details injections:
```typescript
const container = findInReactTree(
  ret,
  (x) =>
    Array.isArray(x?.props?.children) &&
    x?.props?.className?.includes(appDetailsClasses.InnerContainer)
);
```

### Deduplication
Patches can run multiple times due to React re-renders:
```typescript
const gameInfoKey = `unifideck-game-info-${appId}`;
const alreadyHasGameInfo = container.props.children.some(
  (child) => child?.key === gameInfoKey
);

if (!alreadyHasGameInfo) {
  // Only inject once
}
```

### Patch Order
Patches are applied in order of plugin loading:
1. ProtonDB injects at index 1
2. Unifideck InstallInfo injects at index 2 (shifts others)
3. Unifideck GameInfoPanel injects at index 1 (shifts others again)

Final order:
- [0] PlaySection
- [1] GameInfoPanel (Unifideck)
- [2] ProtonMedal (ProtonDB)
- [3] InstallInfoDisplay (Unifideck)
- [4] Tabs

## References

- **decky-frontend-lib**: `/home/deck/Downloads/decky-frontend-lib-main/src/utils/static-classes.ts`
- **ProtonDB Pattern**: `/home/deck/Downloads/protondb-decky-main/src/lib/patchLibraryApp.tsx`
- **Unifideck Implementation**: `/home/deck/Documents/Projects/unifideck-main/unifideck-decky/src/index.tsx`

## Verification Checklist

After implementation:
- [ ] GameInfoPanel appears below play button row
- [ ] GameInfoPanel appears above tabs
- [ ] No overlapping with tabs
- [ ] Gamepad navigation works through panel
- [ ] Responsive on different screen sizes
- [ ] InstallInfoDisplay still works (top-right button)
- [ ] Non-Steam games show panel, Steam games don't
- [ ] ProtonDB medal still appears (if plugin installed)

---

*Document created: 2026-02-02*
*Last updated: 2026-02-02*
