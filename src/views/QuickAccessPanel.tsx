/**
 * QuickAccessPanel — top-level Decky tab content.
 *
 * Replaces the legacy `<Content>` component (1305 LOC)
 * which mixed sync state, account switch logic, downloads,
 * settings, language selection, and a custom tab switcher.
 * Maps each section to its dedicated component
 * (StoreConnections, LibrarySync, StorageSettings,
 * LanguageSelector, DownloadsTab).
 *
 * Tab state is held in a module-level `persistentActiveTab`
 * so the last-viewed tab survives Quick-Access dismount /
 * remount (legacy behaviour from staging index.tsx). Steam's
 * automatic focus pass on mount used to defeat that — see the
 * settle window in the component and `./tab-focus`.
 *
 * Tab buttons are `Focusable`s carrying Steam's own `Tab` /
 * `Selected` classes, inside a `flow-children="row"` row.
 * They are NOT `DialogButton`s: Steam's tab styling assumes a
 * bare element, and DialogButton's own chrome fights it.
 *
 * Each tab needs its own `onActivate` — a `Focusable` with no
 * interactive children is not a focus target without one. The
 * older warning here (that wrapping in an extra `Focusable`
 * swallows focus) applied to wrapping a *DialogButton*; a
 * Focusable-as-tab with `onActivate` is a different construct
 * and does take focus. Verified on-device.
 */
import { CSSProperties, FC, useEffect, useRef, useState } from "react";
import { Focusable, findClassModule } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { shouldSwitchTab, TAB_SETTLE_MS, type ActiveTab } from "./tab-focus";
import {
  StoreConnections,
  LibrarySync,
  LanguageSelector,
  GameDetailsViewModeToggle,
  CollectionsToggle,
  CleanupSection,
  CaptureLogsSection,
  PluginUpdater,
} from "../components/settings";
import { DownloadsTab } from "../components/downloads";

/** Last-viewed tab persisted across QAM mount/unmount. */
let persistentActiveTab: ActiveTab = "settings";

/**
 * Focus-grab retry schedule. The first attempt runs in a `requestAnimation
 * Frame`; if the pill has not taken focus, we retry on this cadence.
 *
 * The old single 140 ms retry was not enough: `HTMLElement.focus()` is a
 * no-op while the element is still hidden behind the Quick-Access open
 * animation, so both attempts could miss and the highlight would end up
 * on a pill that does not match the content being shown.
 */
const GRAB_RETRY_MS = 120;
const GRAB_MAX_ATTEMPTS = 5;

/**
 * Steam's own tab-row CSS module (`TabRow` / `Tab` / `Selected`), looked up at
 * runtime the same way `@decky/ui` resolves Steam internals. Using Steam's
 * real classes means the active tab is highlighted with Steam's styling rather
 * than something we invented, and it tracks Valve's changes for free.
 */
const steamTabClasses = findClassModule(
  (m) => m.TabRowTabs && m.Tab && m.Selected,
) as { Tab?: string; Selected?: string } | undefined;

/**
 * Literal fallback if Steam ever renames that module — these are the values
 * Steam's own `.Tab` / `.Tab.Selected` rules compute to, so the look is
 * identical either way.
 */
const FALLBACK_TAB: CSSProperties = {
  fontSize: 12,
  fontWeight: "bold",
  letterSpacing: "0.5px",
  textTransform: "uppercase",
  background: "transparent",
  color: "#dcdedf",
  borderRadius: 3,
};
const FALLBACK_TAB_SELECTED: CSSProperties = {
  background: "rgba(255, 255, 255, 0.15)",
  color: "#ffffff",
};

/**
 * Geometry for the two tab buttons.
 *
 * The QAM panel is narrow and each button is a fixed 50% (`flex: 1`), so the
 * longest labels — French "Téléchargements" (15 chars) — overran the button's
 * rounded boundary. Tight
 * horizontal padding plus a slightly smaller, *zoom-relative* font (`em`, so it
 * scales with Steam's global UI scale at every resolution) gives the text room
 * to fit; `nowrap` + `overflow: hidden` + `ellipsis` is the safety net so text
 * is clipped *inside* the button (never spills past it) in the extreme case.
 *
 * Previously the active tab was signalled by `fontWeight` + `opacity` alone,
 * which read as "slightly brighter text" rather than "you are on this tab".
 */
const tabButtonStyle = (active: boolean): CSSProperties => ({
  flex: 1,
  minWidth: 0,
  padding: "10px 6px",
  // Steam's `Tab` class is `display: flex` with `text-align: start`, so
  // `textAlign: center` alone does nothing — a flex container positions its
  // children with justify-content (which computes to `normal`, i.e. start),
  // leaving the label hard against the left edge of the pill. Centre it the
  // way the box model actually works, and keep textAlign for the fallback
  // path where the element is not a flex container.
  display: "flex",
  justifyContent: "center",
  alignItems: "center",
  textAlign: "center",
  cursor: "pointer",
  whiteSpace: "nowrap",
  overflow: "hidden",
  textOverflow: "ellipsis",
  ...(steamTabClasses?.Tab
    ? { fontSize: "0.9em" }
    : {
        ...FALLBACK_TAB,
        ...(active ? FALLBACK_TAB_SELECTED : {}),
      }),
});

/** Steam's `Tab` (+ `Selected`) class pair for the active state. */
const tabClassName = (active: boolean): string =>
  [steamTabClasses?.Tab, active ? steamTabClasses?.Selected : null]
    .filter(Boolean)
    .join(" ");

/**
 * Root component of the Decky Loader Quick Access menu.
 * Composes the two tabs (Settings, Downloads) and persists
 * the active tab across QAM open/close.
 */
export const QuickAccessPanel: FC = () => {
  const { t } = useTranslation();
  const [tab, setTabState] = useState<ActiveTab>(persistentActiveTab);

  const setTab = (next: ActiveTab): void => {
    persistentActiveTab = next;
    setTabState(next);
  };

  // ── Surviving a remount on the tab you were actually on ──────────
  //
  // Steam fires a focus event at whichever nav node it picks when the panel
  // mounts, and for this layout that is the FIRST tab pill. Since focus
  // switches tabs (see the comment on the pills below), that automatic pass
  // dragged the panel back to Settings on every remount — and because
  // `setTab` also writes `persistentActiveTab`, it overwrote the remembered
  // tab, so the next open started on Settings too.
  //
  // The visible trigger was confirming an uninstall: in Gaming Mode the QAM
  // renders into its own popup window, so opening a modal tears that window
  // down and closing it mounts this panel afresh — landing the user on
  // Settings mid-task, right after acting on a row in Downloads.
  //
  // Two halves, both needed: ignore Steam's mount pass (it is not the
  // user), and put focus on the pill for the tab we are actually showing so
  // the highlight matches the content.
  //
  // The guard is a time-bounded settle window, NOT a one-shot latch. A latch
  // is spent by whichever focus event arrives first, and which one that is
  // depends on a race between Steam's pass and our own grab below — when
  // both of those missed, the latch was still armed for the user's first
  // stick move and swallowed exactly that move. See ./tab-focus.
  const settleUntil = useRef(performance.now() + TAB_SETTLE_MS);
  const programmatic = useRef(false);
  const settingsPill = useRef<HTMLDivElement>(null);
  const downloadsPill = useRef<HTMLDivElement>(null);

  /**
   * Tab switch driven by focus movement.
   *
   * Bound to BOTH `onFocus` and `onGamepadFocus` on each pill. Steam moves
   * its gamepad ring by toggling the `.gpfocus` class on the nav node, and
   * that does not always drag DOM focus with it — this codebase already
   * treats `.gpfocus`, `:focus` and `:focus-within` as three separate
   * states (see components/play/play.css.ts). Listening to the React DOM
   * event alone therefore missed the ring landing on the other pill, which
   * is why the tab sometimes refused to switch until A was pressed:
   * `onActivate` is dispatched by Steam's nav controller against the
   * `.gpfocus` node, so it worked when `onFocus` had not fired at all.
   * Both signals firing for the same move is harmless: the second one
   * reads the tab the first already set and is rejected as a no-op.
   *
   * `current` reads `persistentActiveTab` rather than the `tab` state,
   * because `setTab` writes it synchronously — the state value would still
   * be the pre-switch one when the second of the two signals arrives in
   * the same tick.
   */
  const focusTab = (next: ActiveTab): void => {
    if (
      !shouldSwitchTab({
        next,
        current: persistentActiveTab,
        programmatic: programmatic.current,
        now: performance.now(),
        settleUntil: settleUntil.current,
      })
    ) {
      return;
    }
    setTab(next);
  };

  // Claim focus for the active pill on mount. Mirrors `PlayShell`'s recipe
  // (rAF plus delayed retries) because a single synchronous focus call
  // loses the race against Steam's own focus pass. Bails once focus has
  // landed, so it can never yank focus the user has since moved.
  useEffect(() => {
    const target =
      persistentActiveTab === "downloads"
        ? downloadsPill.current
        : settingsPill.current;
    if (!target) return;
    let raf = 0;
    let timer = 0;
    let attempts = 0;
    const grab = (): void => {
      // Only ever retried when the previous attempt FAILED to take focus,
      // so this can never claw focus back off something the user moved to.
      if (target.ownerDocument.activeElement === target) return;
      // Flagged so the resulting `focusin` is not mistaken for the user
      // moving onto this pill. `focus()` dispatches it synchronously, so
      // the flag brackets the call exactly.
      programmatic.current = true;
      try {
        target.focus?.();
      } finally {
        programmatic.current = false;
      }
      if (
        target.ownerDocument.activeElement !== target &&
        ++attempts < GRAB_MAX_ATTEMPTS
      ) {
        timer = window.setTimeout(grab, GRAB_RETRY_MS);
      }
    };
    raf = requestAnimationFrame(grab);
    return () => {
      cancelAnimationFrame(raf);
      window.clearTimeout(timer);
    };
  }, []);

  // The tab label carries NO percentage. It used to show the library-SYNC
  // progress, which is a different operation from downloading a game — a tab
  // reading "Downloads (90%)" while nothing is downloading is just wrong, and
  // it also overran the pill. Sync progress belongs to the Library Sync
  // section on the Settings tab, which already reports it.
  const downloadsLabel = t("tabs.downloads");

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {/* Focusable (not DialogButton) so Steam's `Tab` class styles a bare tab
          rather than fighting DialogButton's own button chrome. The row is a
          `flow-children="row"` Focusable so the pair navigates left/right. */}
      <Focusable
        flow-children="row"
        style={{ display: "flex", gap: 6, padding: "4px 8px 0" }}
      >
        {/* Switching on FOCUS, not just on activate: moving the stick onto a
            tab shows that tab immediately, the way Steam's own tab rows
            behave. Requiring an extra A press made navigation feel like it
            had stalled. `onActivate` stays so a click/A press still works
            (and so each Focusable remains a focus target at all).

            BOTH focus signals are bound. `onGamepadFocus` is Steam's own
            nav event, fired when the `.gpfocus` ring lands on the node;
            `onFocus` is the React DOM event, which only fires when DOM
            focus actually moves. The two diverge, which is what made the
            switch intermittent — see `focusTab`. */}
        <Focusable
          ref={settingsPill}
          onFocus={() => focusTab("settings")}
          onGamepadFocus={() => focusTab("settings")}
          onActivate={() => setTab("settings")}
          className={tabClassName(tab === "settings")}
          style={tabButtonStyle(tab === "settings")}
        >
          {t("tabs.settings")}
        </Focusable>
        <Focusable
          ref={downloadsPill}
          onFocus={() => focusTab("downloads")}
          onGamepadFocus={() => focusTab("downloads")}
          onActivate={() => setTab("downloads")}
          className={tabClassName(tab === "downloads")}
          style={tabButtonStyle(tab === "downloads")}
        >
          {downloadsLabel}
        </Focusable>
      </Focusable>
      {/* Spacer wrapper: Steam's PanelSection title carries a negative top
          margin (it assumes it is the first child of the scroll container),
          which otherwise pulls the first section header up into the tab
          buttons above. Padding here pushes the content clear of the row. */}
      <div style={{ paddingBlockStart: 12 }}>
        {tab === "settings" && (
          <>
            <StoreConnections />
            <LibrarySync />
            <LanguageSelector />
            <GameDetailsViewModeToggle />
            <CollectionsToggle />
            <PluginUpdater />
            <CleanupSection />
            <CaptureLogsSection />
          </>
        )}
        {tab === "downloads" && <DownloadsTab />}
      </div>
    </div>
  );
};
