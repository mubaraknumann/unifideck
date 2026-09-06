/**
 * Library grid-tile store badge — render each non-Steam (Unifideck)
 * shortcut's STORE logo (Epic/GOG/Amazon/Xbox/Ubisoft) in place of the
 * Steam Deck 'D' glyph on its library tile, keeping the compatibility
 * status icon ("Proton badge") next to it.
 *
 * Steam hides the Deck-compat badge for shortcuts
 * (`overview.BIsModOrShortcut() ? null : …`), so non-Steam tiles show
 * no corner badge. This patches the library-tile component so our
 * shortcuts get a badge that matches native styling.
 *
 * HOW (verified live via CEF/CDP):
 *  - The tile is the exported `We` memo (module factory source contains
 *    `LibraryItemIcons` + `BIsModOrShortcut` + `BIsMusicAlbum`). It's a
 *    `React.memo` whose `.type` (the mobx-observer render fn) is a
 *    writable own-property — we patch THAT (never the non-configurable
 *    module export). The memo object is shared across SharedJSContext +
 *    the BPM window and renders both the portrait and wide grid.
 *  - The badge component `C.$o` (factory source contains
 *    `SteamDeckCompatInfo`) renders
 *    `<div class={SteamDeckCompatInfo,…}>[ <DeckD/>, <StatusIcon/> ]</div>`.
 *    We reuse its output (so the status icon comes for free) and swap
 *    `children[0]` (the Deck 'D') for a `<StoreIcon>`.
 *  - Bottom-right placement + show-on-highlight are pure CSS keyed on the
 *    `SteamDeckCompatIcon` class (`margin-inline-start:auto`,
 *    `opacity:0` + a `:hover` rule). We pass that class (with
 *    `DeckCompat`) to `C.$o`, exactly as native does, so we inherit both.
 *
 * SAFETY: this only READS `BIsModOrShortcut()` (to pick which tiles to
 * decorate) and ADDS an element to the rendered output. It never writes
 * `appStore`, never proxies the overview — so launch routing (RunGame),
 * which reads the real overview, is unaffected. `StoreBadge` returns
 * `null` on any error so it can never break the grid.
 */
import {
  createElement,
  cloneElement,
  Fragment,
  type FC,
  type ReactElement,
} from "react";
import { findInReactTree } from "./react-tree";
import { StoreIcon } from "../../components/shared/StoreIcon";
import { getStoreForApp } from "../library-filters";
import { getFacet } from "../library-facets";
import { activeCompatTrack } from "../device-type";
import { overviewCompatCategory } from "./compat-packed";
import type { StoreId } from "../../types/api";

const REACT_MEMO = Symbol.for("react.memo");

/** Minimal shape of a Steam library-tile overview we read. */
interface TileOverview {
  appid: number;
  app_type: number;
  /** Per-device compat, 2 bits per track — see `compat-packed.ts`. */
  steam_hw_compat_category_packed?: number;
  BIsModOrShortcut?: () => boolean;
}
type TileProps = { app?: TileOverview };
type TileRenderFn = (this: unknown, ...args: unknown[]) => ReactElement;
interface ReactMemoObject {
  $$typeof: symbol;
  type: TileRenderFn;
  __unifideckOrigType?: TileRenderFn;
}
/**
 * `wrappedTileType` with its self-identification brand + a durable
 * back-reference to the memo it's installed on. The brand lets install
 * detect an already-wrapped memo (across plugin reloads, where the module
 * singletons reset but the webpack memo persists); the back-reference lets
 * the wrapper recover the true original at render time without depending on
 * the mutable module-level `originalType`.
 */
interface WrappedTileFn extends TileRenderFn {
  __unifideckWrapper?: true;
  __unifideckMemo?: ReactMemoObject;
}
/** `C.$o` — the deck-compat badge. Pure fn (no hooks), safe to call. */
type DeckCompatBadgeFn = (props: {
  category: number;
  className: string;
}) => ReactElement | null;
interface WebpackRequire {
  (id: string): unknown;
  m: Record<string, unknown>;
}

// ── resolved-once singletons ──────────────────────────────
let started = false;
let memoObject: ReactMemoObject | null = null;
let originalType: TileRenderFn | null = null;
let deckCompatBadge: DeckCompatBadgeFn | null = null;
/** Resolved obfuscated CSS classes (rotate per Steam build). */
let rowClass = ""; // LibraryItemIcons — the icon row we inject into
let deckClass = ""; // DeckCompat + SteamDeckCompatIcon — drives bottom-right + show-on-highlight
let infoClass = ""; // SteamDeckCompatInfo — the badge container (cat==0 fallback)

const warned = new Set<string>();
function warnOnce(reason: string, extra?: unknown): void {
  if (warned.has(reason)) return;
  warned.add(reason);
  console.warn(`[Unifideck TileBadge] ${reason}`, extra ?? "");
}

function getWebpackRequire(): WebpackRequire | null {
  const w = window as unknown as { webpackChunksteamui?: unknown[] };
  if (!Array.isArray(w.webpackChunksteamui)) return null;
  let req: WebpackRequire | undefined;
  try {
    w.webpackChunksteamui.push([
      [`unifideck_tilebadge_${Math.random()}`],
      {},
      (r: WebpackRequire) => {
        req = r;
      },
    ] as never);
  } catch {
    return null;
  }
  return req && req.m ? req : null;
}

/** Find the tile module by its factory source (immune to runtime
 *  patching of instantiated exports) and return its `React.memo`. */
function findTileMemo(req: WebpackRequire): ReactMemoObject | null {
  for (const id of Object.keys(req.m)) {
    let src = "";
    try {
      src = String(req.m[id]);
    } catch {
      continue;
    }
    if (
      !src.includes("LibraryItemIcons") ||
      !src.includes("BIsModOrShortcut") ||
      !src.includes("BIsMusicAlbum")
    ) {
      continue;
    }
    let exports: Record<string, unknown>;
    try {
      exports = req(id) as Record<string, unknown>;
    } catch {
      continue;
    }
    for (const key of Object.keys(exports)) {
      let v: unknown;
      try {
        v = exports[key];
      } catch {
        continue;
      }
      const memo = v as ReactMemoObject | null;
      if (
        memo &&
        memo.$$typeof === REACT_MEMO &&
        typeof memo.type === "function"
      ) {
        return memo;
      }
    }
  }
  return null;
}

function strProp(o: unknown, key: string): string | null {
  try {
    const v = (o as Record<string, unknown>)[key];
    return typeof v === "string" ? v : null;
  } catch {
    return null;
  }
}

/** Resolve `C.$o` + the CSS classes in one pass over the bundle. */
function resolveBadgeRefs(req: WebpackRequire): boolean {
  let iconCls: string | null = null;
  let deckCompatCls: string | null = null;
  for (const id of Object.keys(req.m)) {
    let exports: unknown;
    try {
      exports = req(id);
    } catch {
      continue;
    }
    if (!exports || typeof exports !== "object") continue;
    for (const o of [exports, (exports as { default?: unknown }).default]) {
      if (!o || typeof o !== "object") continue;
      if (!rowClass) {
        const lii = strProp(o, "LibraryItemIcons");
        const sdci = strProp(o, "SteamDeckCompatIcon");
        if (lii && sdci) {
          rowClass = lii;
          iconCls = sdci;
        }
      }
      if (!deckCompatCls) deckCompatCls = strProp(o, "DeckCompat");
      if (!infoClass) infoClass = strProp(o, "SteamDeckCompatInfo") ?? "";
    }
    if (!deckCompatBadge) {
      let keys: string[] = [];
      try {
        keys = Object.keys(exports as object);
      } catch {
        keys = [];
      }
      for (const k of keys) {
        let v: unknown;
        try {
          v = (exports as Record<string, unknown>)[k];
        } catch {
          continue;
        }
        if (typeof v !== "function") continue;
        let src = "";
        try {
          src = v.toString();
        } catch {
          continue;
        }
        if (src.includes("SteamDeckCompatInfo") && src.length < 500) {
          deckCompatBadge = v as DeckCompatBadgeFn;
          break;
        }
      }
    }
    if (rowClass && iconCls && deckCompatCls && infoClass && deckCompatBadge) {
      break;
    }
  }
  if (!rowClass || !iconCls) return false;
  // DeckCompat + SteamDeckCompatIcon — the className native passes; the
  // SteamDeckCompatIcon part is what the CSS rule keys on for placement.
  deckClass = (deckCompatCls ? `${deckCompatCls} ` : "") + iconCls;
  return !!deckCompatBadge;
}

/**
 * The injected badge. For shortcuts WITH compat data it reuses Steam's
 * own `C.$o` output (correct container + status icon) and swaps the
 * Deck-'D' (`children[0]`) for the store logo; for shortcuts without
 * compat it renders the store logo alone in a matching container. Any
 * failure returns `null` (no badge) so the tile/grid can't break.
 */
const StoreBadge: FC<{ category: number; store: StoreId }> = ({
  category,
  store,
}) => {
  try {
    const logo = createElement(StoreIcon, { store, size: 16, color: "#fff" });
    if (category > 0 && deckCompatBadge) {
      const base = deckCompatBadge({ category, className: deckClass });
      if (base && base.props) {
        const kids = (base.props as { children?: unknown }).children;
        const status = Array.isArray(kids) ? kids[1] : null;
        return cloneElement(base, undefined, logo, status);
      }
    }
    return createElement(
      "div",
      { className: `${infoClass} ${deckClass}` },
      logo,
    );
  } catch {
    return null;
  }
};

const BADGE_KEY = "unifideck-store-badge";

/**
 * Resolve the true original tile render fn at render time. The library
 * `React.memo` is a webpack singleton that survives plugin reload, but our
 * module-level `originalType` resets to `null` on every fresh module
 * instance — so a wrapper installed by a prior instance would read a null
 * `originalType` and crash. Recover from the durable stash on the memo
 * itself first (survives reload), then the render-time `this`, then the
 * module vars. Never return the wrapper itself (would recurse). Returns
 * `null` only if nothing usable is found, in which case the caller renders
 * an empty Fragment rather than crashing the whole grid.
 */
function resolveOriginal(self: unknown): TileRenderFn | null {
  const candidates: Array<TileRenderFn | undefined> = [
    (wrappedTileType as WrappedTileFn).__unifideckMemo?.__unifideckOrigType,
    (self as ReactMemoObject | null | undefined)?.__unifideckOrigType,
    originalType ?? undefined,
    memoObject?.__unifideckOrigType,
  ];
  for (const c of candidates) {
    if (typeof c === "function" && c !== (wrappedTileType as TileRenderFn)) {
      return c;
    }
  }
  return null;
}

function wrappedTileType(this: unknown, ...args: unknown[]): ReactElement {
  const orig = resolveOriginal(this);
  if (!orig) {
    // Transient reload artifact: the wrapper is still installed on the
    // shared memo but the original can't be recovered. Render nothing for
    // this one tile — an empty Fragment is a valid element that can't trip
    // the mobx-observer render machinery — instead of throwing to Steam's
    // error boundary and taking down the entire library grid. Self-heals
    // on the next full render once install rebinds the original.
    warnOnce("original type unavailable; rendering empty tile");
    return createElement(Fragment);
  }
  const props = args[0] as TileProps | undefined;
  const app = props?.app;
  // Fast bail for native tiles — only READ BIsModOrShortcut, never write.
  if (
    !app ||
    typeof app.BIsModOrShortcut !== "function" ||
    !app.BIsModOrShortcut()
  ) {
    return orig.apply(this, args);
  }
  const store = getStoreForApp(app.appid, app.app_type);
  if (!store || store === "steam") return orig.apply(this, args);

  let ret: ReactElement;
  try {
    ret = orig.apply(this, args);
  } catch {
    return orig.apply(this, args);
  }
  try {
    const row = findInReactTree(ret, (n) => {
      const cls = (n as { props?: { className?: unknown } })?.props?.className;
      return typeof cls === "string" && cls.includes(rowClass);
    }) as { props?: { children?: unknown } } | null;
    if (row?.props) {
      // Steam's own value for THIS device's bits, else the backend's
      // already-resolved category for our shortcut.
      const category =
        overviewCompatCategory(app, activeCompatTrack()) ||
        getFacet(app.appid)?.compat_category ||
        0;
      const badge = createElement(StoreBadge, {
        key: BADGE_KEY,
        category,
        store: store as StoreId,
      });
      const kids = row.props.children;
      if (Array.isArray(kids)) {
        if (
          !kids.some((c) => (c as { key?: unknown } | null)?.key === BADGE_KEY)
        ) {
          kids.push(badge);
        }
      } else if (kids != null) {
        row.props.children = [kids, badge];
      } else {
        row.props.children = [badge];
      }
    }
  } catch (e) {
    warnOnce("inject failed", e);
  }
  return ret;
}

/**
 * Install the patch at boot (before the grid first mounts — `React.memo`
 * caches the resolved inner fn on already-mounted fibers, so boot-time
 * install avoids forced remounts). Returns a disposer. Degrades to a
 * no-op (grid untouched) if the tile component or classes can't be
 * resolved on this Steam build.
 */
export function startTileStoreBadgePatch(): () => void {
  if (started) return stopTileStoreBadgePatch;
  started = true;
  try {
    const req = getWebpackRequire();
    if (!req) {
      warnOnce("webpack require unavailable; tile badges disabled");
      started = false;
      return () => {};
    }
    const memo = findTileMemo(req);
    if (!memo) {
      warnOnce("library-tile memo not found; tile badges disabled");
      started = false;
      return () => {};
    }
    if (!resolveBadgeRefs(req)) {
      warnOnce(
        "deck-compat badge / classes not resolved; tile badges disabled",
      );
      started = false;
      return () => {};
    }
    memoObject = memo;
    // Recover the true original. `memo` is a webpack singleton that
    // survives plugin reload, so on re-inject a prior module instance's
    // wrapper may already sit in `memo.type` with the durable stash on
    // `__unifideckOrigType`. Prefer the stash; never treat our own wrapper
    // as the original (would recurse). If `memo.type` is a stale wrapper
    // with no stash to recover from, we can't safely unwrap it — leave it
    // and bail rather than double-wrapping or stashing the wrapper.
    const currentType = memo.type as WrappedTileFn;
    if (typeof memo.__unifideckOrigType === "function") {
      originalType = memo.__unifideckOrigType;
    } else if (!currentType.__unifideckWrapper) {
      originalType = memo.type;
    } else {
      warnOnce("stale wrapper without recoverable original; leaving as-is");
      // Rebind this module's back-reference so the live (stale) wrapper's
      // resolver can still find whatever original a later install stashes.
      (wrappedTileType as WrappedTileFn).__unifideckMemo = memo;
      started = false;
      return () => {};
    }
    memo.__unifideckOrigType = originalType;
    const wrapper = wrappedTileType as WrappedTileFn;
    wrapper.__unifideckWrapper = true;
    wrapper.__unifideckMemo = memo;
    memo.type = wrappedTileType;
    console.log("[Unifideck] Tile store-badge patch active");
  } catch (e) {
    console.error("[Unifideck] tile store-badge patch install failed:", e);
    started = false;
    return () => {};
  }
  return stopTileStoreBadgePatch;
}

export function stopTileStoreBadgePatch(): void {
  if (!started) return;
  const memo = memoObject;
  const orig = originalType;
  // Reset the module singletons first — teardown must be idempotent and a
  // second call must not re-enter this body.
  memoObject = null;
  originalType = null;
  started = false;
  if (!memo || !orig) return;
  try {
    // Only restore OUR wrapper (never clobber a foreign patch that may have
    // been layered on since install).
    if (memo.type === (wrappedTileType as TileRenderFn)) {
      memo.type = orig;
    }
    // Delete the recovery stash ONLY once the live slot is confirmed
    // unwrapped. If the restore above threw or silently didn't take (e.g.
    // `.type` became non-writable on a newer Steam build), leave
    // `__unifideckOrigType` in place so the still-live wrapper's resolver
    // can recover the original next render and a later install can rebind.
    if (memo.type !== (wrappedTileType as TileRenderFn)) {
      delete memo.__unifideckOrigType;
    }
  } catch (e) {
    console.warn("[Unifideck TileBadge] unpatch failed:", e);
  }
}
