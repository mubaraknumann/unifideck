/**
 * AppDetailsPatch — patches Steam's App Details React tree.
 *
 * Steam renders `/library/app/<appId>` via a route whose
 * `renderFunc` produces the actual app-details tree. We
 * intercept that renderFunc with `afterPatch` and inject two
 * Unifideck components into Steam's `InnerContainer` :
 *
 *   1. `<PlaySectionWrapper>` — replaces / decorates the
 *       native Play / Install row for non-Steam shortcuts.
 *   2. `<GameInfoPanel>`    — metadata + scores panel
 *       rendered right after the PlaySection wrapper.
 *
 * The patch is registered via SteamBridge's router patch ;
 * the returned handle is held by the plugin entry and
 * `.remove()`'d on plugin unload.
 *
 * Notes on robustness :
 *  - Anchors via CSS class names (`appDetailsClasses?.InnerContainer`,
 *    `playSectionClasses?.Container`), NOT React `displayName`
 *    which Steam mangles in production builds.
 *  - `__unifideckPatched` marker on `renderFunc` prevents
 *    double-patching when ProtonDB/HLTB co-patch the same
 *    route (they use their own marker, `__deckyPatch`).
 *  - Position-correction : if our injected element drifts
 *    past index 3 after a Steam restart, splice it out and
 *    re-insert at the anchored index.
 *  - Non-Steam-game gate (`appId > 2_000_000_000`) so we
 *    only override shortcuts, never first-party Steam games.
 */
import {
  afterPatch,
  createReactTreePatcher,
  appDetailsClasses,
  appDetailsHeaderClasses,
  playSectionClasses,
} from "@decky/ui";
import { SteamBridge, type RouterPatchHandle } from "../lib/steam-bridge";
import { findInReactTree } from "../lib/steam-bridge/react-tree";
import {
  isUnifideckGame,
  isUnifideckCacheLoaded,
} from "../lib/library-filters";
import { getGameStateVersion } from "../lib/game-state-version";
import { reinjectMetadataWhenLoaded } from "../lib/steam-bridge/app-store-patcher";
import { shouldPatchShortcut } from "../lib/steam-bridge/shortcut-ownership";
import { DownloadProvider } from "../contexts/DownloadContext";
import { PlaySectionWrapper } from "../components/play";
import { HIDE_NATIVE_PLAY_MARKER } from "../components/play/play.css";
import { GameInfoPanel } from "../components/info";

/** Stub component that's only used in the React DevTools
 *  display name. Helps debugging in production builds. */
export const AppDetailsPatch = (): null => null;
AppDetailsPatch.displayName = "Unifideck.AppDetailsPatch";

/** Apply the patch via the given bridge. Returns a handle
 *  whose `.remove()` undoes both injections. */
export function applyAppDetailsPatch(bridge: SteamBridge): RouterPatchHandle {
  return bridge.addRouterPatch("/library/app/:appid", (routerTree: unknown) => {
    const routeProps = findInReactTree<RouteProps>(
      routerTree,
      (x) => (x as RouteProps | null)?.renderFunc != null,
    );
    if (!routeProps) return routerTree;

    // Skip if we've already wrapped this renderFunc. The marker is
    // Unifideck-specific so it doesn't collide with ProtonDB / HLTB.
    if ((routeProps.renderFunc as PatchedFn).__unifideckPatched) {
      return routerTree;
    }

    const patchHandler = createReactTreePatcher(
      [
        (tree: unknown) =>
          findInReactTree<NodeWithChildren>(
            tree,
            (x) =>
              (x as NodeWithOverview | null)?.props?.children?.props
                ?.overview != null,
          )?.props?.children as unknown,
      ],
      (_args: unknown[], ret: unknown) => {
        try {
          injectIntoTree(ret);
        } catch (e) {
          console.error("[Unifideck] AppDetailsPatch handler error:", e);
        }
        return ret;
      },
    );

    afterPatch(routeProps, "renderFunc", patchHandler);
    (routeProps.renderFunc as PatchedFn).__unifideckPatched = true;
    return routerTree;
  });
}

/** Walk the rendered route tree and splice our components
 *  into the `InnerContainer` for non-Steam shortcuts. */
function injectIntoTree(ret: unknown): void {
  const overviewNode = findInReactTree<NodeWithOverview>(
    ret,
    (x) =>
      (x as NodeWithOverview | null)?.props?.children?.props?.overview != null,
  );
  const overview = overviewNode?.props?.children?.props?.overview;
  if (!overview) return;
  const appId = overview.appid;

  // Only override non-Steam shortcuts (appId > 2 billion).
  if (!(appId > 2_000_000_000)) return;

  // Only override Unifideck-managed games — never the user's own
  // non-Steam shortcuts (EmulationStationDE, Firefox, ...). This single gate
  // covers the hide marker, the Play wrapper, and the GameInfo panel.
  //
  // Two signals, because neither is enough alone: the cache is authoritative
  // but says nothing until an RPC lands (and retries with backoff on
  // failure), while Steam's own shortcut Exe is synchronous but only present
  // once app details are loaded. Staying optimistic on the cache ALONE meant
  // that during that window every non-Steam shortcut got its native Play row
  // and whole tabbed section hidden — a blanked App-Details page for someone
  // else's shortcut, and worse the more shortcuts a user has (719 on the
  // device that surfaced it, 27 of them not ours).
  if (
    !shouldPatchShortcut(
      appId,
      isUnifideckCacheLoaded(),
      isUnifideckGame(appId),
    )
  ) {
    return;
  }

  // Trigger Steam-Store metadata spoofing for this shortcut so
  // Steam's own UI (capsule image, tile, presence) renders the
  // matched Steam game. Fire-and-forget — the patcher reads from
  // its in-memory cache; the backend RPC is a no-op stub.
  void reinjectMetadataWhenLoaded(appId);

  const innerContainer = findInReactTree<NodeWithChildren>(ret, (x) => {
    const n = x as NodeWithChildren | null;
    return (
      Array.isArray(n?.props?.children) &&
      typeof n?.props?.className === "string" &&
      appDetailsClasses?.InnerContainer != null &&
      n.props.className.includes(appDetailsClasses.InnerContainer)
    );
  });

  // If the container isn't ready yet, skip — the patcher fires
  // again on the next render.
  if (
    !innerContainer ||
    !innerContainer.props ||
    !Array.isArray(innerContainer.props.children)
  )
    return;

  // Mark the inner container so our scoped CSS rule (nativePlayHideCss)
  // hides Steam's native Play row — which renders in a *separate* subtree
  // beneath this same container, so it can't be removed in-tree. Set
  // synchronously here (re-applied every render; idempotent) instead of
  // the old async CDP `display:none`. Only shortcuts reach this code, so
  // native Steam games are never marked.
  const containerClassName =
    typeof innerContainer.props.className === "string"
      ? innerContainer.props.className
      : "";
  if (!containerClassName.includes(HIDE_NATIVE_PLAY_MARKER)) {
    innerContainer.props.className =
      `${containerClassName} ${HIDE_NATIVE_PLAY_MARKER}`.trim();
  }

  const children = innerContainer.props!.children as unknown[];
  const playWrapperKey = `unifideck-play-wrapper-${appId}`;
  const gameInfoKey = `unifideck-game-info-${appId}`;
  const version = getGameStateVersion(appId);

  injectPlayWrapper(children, appId, playWrapperKey, version);
  injectGameInfoPanel(children, appId, playWrapperKey, gameInfoKey, version);
  relocateHltbAbovePlay(children, playWrapperKey);
}

/** Relocate HLTB for Deck's stats overlay (`#hltb-for-deck`) to the
 *  hero/Play seam — the same spot it occupies in the *native* Steam UI
 *  (right after the header, before the content panel).
 *
 *  On a native game HLTB splices its element just before Steam's content
 *  panel, so it lands at the hero bottom and its own CSS (transparent bar
 *  for `default`/`clean-default`, hero-anchored box for `clean`/
 *  `clean-left`) renders correctly. In our layout the same splice point
 *  lands LOW (after our injected Play + GameInfo, before the hidden tabbed
 *  section), so the overlay drifts down onto our custom buttons — that
 *  drift, not HLTB's styling, is the bug. Moving its wrapper back to the
 *  seam makes the `default` / `clean-default` BAR modes render pixel-
 *  identical to native with ZERO style changes. The `clean` / `clean-left`
 *  BOX modes get one tiny position-only nudge ({@link HLTB_CLEAN_BOX_POSITION_CSS})
 *  because HLTB's own `top:-55vh` overshoots the box off-screen from the
 *  seam; everything else (transparency, size, side) stays HLTB's own.
 *
 *  Safe because our route patch is the OUTERMOST renderFunc wrapper (we
 *  register after HLTB), so its element is already in `children` when this
 *  runs. No-op — and therefore graceful — when HLTB isn't installed /
 *  co-patching. */
function relocateHltbAbovePlay(
  children: unknown[],
  playWrapperKey: string,
): void {
  const hltbIdx = children.findIndex((c) => idOf(c) === "hltb-for-deck");
  if (hltbIdx === -1) return; // HLTB not present this render
  const playIdx = children.findIndex((c) =>
    keyOf(c).startsWith(playWrapperKey),
  );
  if (playIdx === -1) return; // our Play wrapper not placed this render
  if (hltbIdx === playIdx - 1) return; // already directly above — idempotent

  const [hltbEl] = children.splice(hltbIdx, 1);
  // Recompute the Play wrapper index — the splice above shifts it when
  // HLTB sat before it.
  const newPlayIdx = children.findIndex((c) =>
    keyOf(c).startsWith(playWrapperKey),
  );
  if (newPlayIdx === -1) {
    children.splice(hltbIdx, 0, hltbEl); // lost the anchor — don't drop HLTB
    return;
  }
  children.splice(newPlayIdx, 0, hltbEl);
}

function injectPlayWrapper(
  children: unknown[],
  appId: number,
  baseKey: string,
  version: number,
): void {
  const existingIdx = children.findIndex((c) => keyOf(c).startsWith(baseKey));

  if (existingIdx === -1) {
    const idx = findPlaySectionInsertIndex(children);
    if (idx < 0) {
      // Anchor not found yet — skip injection this render. The
      // patcher re-fires on the next Steam render; the action-bar
      // container usually arrives within one tick. We deliberately
      // don't fall back to a synthetic "after first child" index
      // because that lands the wrapper in the top strip (next to
      // UNSUPPORTED / Details / Synopsis).
      return;
    }
    // Wrap in DownloadProvider — the only context the
    // injected components need (useDownloads in
    // GameInfoCompatRow and usePlaySection). Steam's React
    // tree is rendered outside our RootProvider.
    children.splice(
      idx,
      0,
      <DownloadProvider key={`${baseKey}-v${version}`}>
        <PlaySectionWrapper appId={appId} />
      </DownloadProvider>,
    );
    return;
  }

  // Position-correction : reposition if drifted past index 3.
  if (existingIdx > 3) {
    const [el] = children.splice(existingIdx, 1);
    const correctIdx = findPlaySectionInsertIndex(children);
    if (correctIdx < 0) {
      // Re-insert at the original index — we lost the anchor
      // mid-frame ; don't lose the wrapper entirely.
      children.splice(existingIdx, 0, el);
      return;
    }
    children.splice(correctIdx, 0, el);
  }
}

function injectGameInfoPanel(
  children: unknown[],
  appId: number,
  wrapperKey: string,
  baseKey: string,
  version: number,
): void {
  const existingIdx = children.findIndex((c) => keyOf(c).startsWith(baseKey));

  if (existingIdx === -1) {
    const wrapperIdx = children.findIndex((c) =>
      keyOf(c).startsWith(wrapperKey),
    );
    let idx: number;
    if (wrapperIdx >= 0) {
      idx = wrapperIdx + 1;
    } else {
      const anchor = findPlaySectionInsertIndex(children);
      if (anchor < 0) return; // wait for next render — see injectPlayWrapper
      idx = anchor + 1;
    }
    // DownloadProvider needed by GameInfoCompatRow's useDownloads.
    children.splice(
      idx,
      0,
      <DownloadProvider key={`${baseKey}-v${version}`}>
        <GameInfoPanel appId={appId} />
      </DownloadProvider>,
    );
    return;
  }

  // Position-correction : keep panel immediately after wrapper.
  const wrapperIdx = children.findIndex((c) => keyOf(c).startsWith(wrapperKey));
  if (wrapperIdx >= 0 && existingIdx !== wrapperIdx + 1) {
    const [el] = children.splice(existingIdx, 1);
    const newWrapperIdx = children.findIndex((c) =>
      keyOf(c).startsWith(wrapperKey),
    );
    children.splice(newWrapperIdx + 1, 0, el);
  }
}

/** Resolve where PlaySectionWrapper should land in the
 *  `InnerContainer`'s children array. Tries (in order) :
 *    1. Right after the native PlaySection container.
 *    2. Right after the AppDetails header / TopCapsule.
 *  Returns `-1` if neither anchor is present — caller skips
 *  injection this render and retries on the next one. We
 *  *intentionally* do NOT fall back to a synthetic position
 *  (e.g. "after first non-Unifideck child") because Steam's
 *  InnerContainer also holds the top action strip
 *  (UNSUPPORTED / Details / Synopsis) and the wrong fallback
 *  plants the wrapper there.
 */
function findPlaySectionInsertIndex(children: unknown[]): number {
  if (playSectionClasses?.Container) {
    const idx = children.findIndex((c) =>
      classOf(c).includes(playSectionClasses.Container),
    );
    if (idx >= 0) return idx + 1;
  }

  const headerIdx = children.findIndex((c) => {
    const cn = classOf(c);
    return (
      (appDetailsHeaderClasses?.TopCapsule &&
        cn.includes(appDetailsHeaderClasses.TopCapsule)) ||
      (appDetailsClasses?.Header && cn.includes(appDetailsClasses.Header))
    );
  });
  if (headerIdx >= 0) return headerIdx + 1;

  return -1;
}

function keyOf(node: unknown): string {
  return (node as { key?: string } | null)?.key ?? "";
}

/** Read a React element's `props.id` (used to find HLTB's
 *  `id="hltb-for-deck"` element spliced into our container). */
function idOf(node: unknown): string {
  const id = (node as { props?: { id?: unknown } } | null)?.props?.id;
  return typeof id === "string" ? id : "";
}

function classOf(node: unknown): string {
  const cn = (node as NodeWithChildren | null)?.props?.className;
  return typeof cn === "string" ? cn : "";
}

interface RouteProps {
  renderFunc: (...args: unknown[]) => unknown;
}

type PatchedFn = ((...args: unknown[]) => unknown) & {
  __unifideckPatched?: boolean;
};

interface NodeWithChildren {
  props?: {
    children?: unknown;
    className?: unknown;
    [k: string]: unknown;
  };
}

interface NodeWithOverview {
  props?: {
    children?: {
      props?: { overview?: { appid: number } };
    };
  };
}
