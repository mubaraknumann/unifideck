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
 * remount (legacy behaviour from staging index.tsx).
 *
 * Tab switching itself is delegated to `@decky/ui`'s `Tabs`
 * component — the same one Steam uses for the Library and
 * Media pages — instead of a hand-rolled `Focusable` row. That
 * gets SteamOS's own left/right controller toggling, focus
 * handling and styling for free, and retires the settle-window
 * workaround the old focus-driven pills needed (see the removed
 * `./tab-focus`) — `Tabs` is a controlled component keyed off
 * `activeTab`/`onShowTab`, not DOM focus events, so Steam's
 * mount-time focus pass no longer has a path to change tabs.
 *
 * `Tabs` renders its content pane with `position: absolute`,
 * which needs an ancestor with a *definite* height to fill.
 * On the Library/Media pages that ancestor is the full-page
 * layout; inside Decky's Quick-Access popup, the per-plugin
 * wrapper Decky renders around us (title bar + back button)
 * auto-sizes to content instead, so there is no definite height
 * anywhere above `Tabs` — it collapsed to a ~50px sliver with
 * the rest of the panel left blank. `rootRef`/`height` below
 * measure the actual Quick-Access content slot at runtime
 * (`#quickaccess_content_<n>`, sized by Decky itself, not by
 * our content — no circularity) and set an explicit height on
 * our own wrapper so `Tabs` has something real to anchor to.
 * Verified on-device: without this, `styles`/`dom` on
 * `QuickAccess_uid*` showed the tab content wrapper stuck at
 * `height: 50px` regardless of the ~390px actually available.
 *
 * `Tabs` is also laid out for a full page: side padding on the
 * header and on the content pane, and spacers around the tab
 * row. In the 300px QAM that cost the content 48px of width and
 * clipped the second tab. `TABS_CSS` puts the geometry back to
 * what the hand-rolled row had: 4px/8px header padding, two
 * half-width tabs 6px apart, a 12px gap above full-width
 * content. The L1/R1 glyphs stay: Steam shows them only while
 * the tab row has focus, and the tabs share what is left.
 */
import { FC, useLayoutEffect, useRef, useState } from "react";
import { Tabs, findClassModule } from "@decky/ui";
import { useTranslation } from "react-i18next";
import {
  StoreConnections,
  LibrarySync,
  LanguageSelector,
  GameDetailsViewModeToggle,
  CollectionsToggle,
  StoreOwnershipToggle,
  CleanupSection,
  CaptureLogsSection,
  PluginUpdater,
} from "../components/settings";
import { DownloadsTab } from "../components/downloads";
import { QamTabLabel } from "./QamTabLabel";

/** The two Quick-Access tabs. */
type ActiveTab = "settings" | "downloads";

/** Last-viewed tab persisted across QAM mount/unmount. */
let persistentActiveTab: ActiveTab = "settings";

/** Decky's Quick-Access content slot for the active plugin tab. */
const QUICKACCESS_SLOT_SELECTOR = '[id^="quickaccess_content_"]';

/** Scope class on our root, so the overrides below touch only this panel. */
const ROOT_CLASS = "unifideck-qam-tabs";

/**
 * Steam's tabbed-page CSS module, the one `Tabs` renders with. Its class
 * names are hashed per build, so they are looked up at runtime.
 */
const tabClasses = findClassModule(
  (m) => m.TabContentsScroll && m.TabRowTabs,
) as Record<string, string> | undefined;

/**
 * Pre-`Tabs` QAM geometry, applied over Steam's full-page layout.
 *
 * Content starts at 58px: the 46px header (4px top padding + 42px tab)
 * plus the 12px gap the old row kept above the first section.
 * Empty if Steam ever renames the module: the panel then keeps Steam's
 * own (wider) spacing rather than breaking.
 */
const TABS_CSS = ((c) => {
  if (!c) return "";
  const s = `.${ROOT_CLASS}`;
  return `
    ${s} .${c.TabHeaderRowWrapper} { padding: 4px 8px 0 !important; }
    ${s} .${c.TabRowSpacer} { display: none !important; }
    ${s} .${c.TabRow}, ${s} .${c.TabRowTabs}, ${s} .${c.TabsRowScroll} { width: 100%; }
    ${s} .${c.FixCenterAlignScroll} {
      display: flex; gap: 6px; width: 100%; padding: 0 !important;
    }
    ${s} .${c.Tab} {
      flex: 1; min-width: 0; padding: 10px 6px !important;
      justify-content: center; font-size: 0.9em;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    ${s} .${c.TabTitle} { flex: 1; min-width: 0; overflow: hidden; }
    ${s} .${c.TabContentsScroll} { padding: 58px 0 40px !important; }
  `;
})(tabClasses);

/**
 * Root component of the Decky Loader Quick Access menu.
 * Composes the two tabs (Settings, Downloads) and persists
 * the active tab across QAM open/close.
 */
export const QuickAccessPanel: FC = () => {
  const { t } = useTranslation();
  const [tab, setTabState] = useState<ActiveTab>(persistentActiveTab);
  const rootRef = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState<number>();

  const setTab = (next: ActiveTab): void => {
    persistentActiveTab = next;
    setTabState(next);
  };

  // Give `Tabs` a definite height to fill (see the header comment). Measured
  // against the Quick-Access slot rather than our own parent, because our
  // parent auto-sizes to US — measuring it would be circular. The slot's
  // own height comes from Decky, not from our content, so it is safe to
  // subtract whatever sits above us in it and re-measure on resize, which
  // also covers the Deck/desktop QAM size difference.
  //
  // "Whatever sits above us" is measured as our offset inside the slot, not
  // as a sibling's height: Decky puts its title bar beside a padded wrapper
  // around us, so we have no previous sibling and the old sibling lookup
  // subtracted 0, overflowing the slot by the title bar + 16px.
  useLayoutEffect(() => {
    const root = rootRef.current;
    const slot = root?.closest<HTMLElement>(QUICKACCESS_SLOT_SELECTOR);
    if (!root || !slot) return;
    const measure = (): void => {
      const offset =
        root.getBoundingClientRect().top -
        slot.getBoundingClientRect().top +
        slot.scrollTop;
      setHeight(Math.max(0, slot.clientHeight - offset));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(slot);
    return () => observer.disconnect();
  }, []);

  // The tab label carries NO percentage. It used to show the library-SYNC
  // progress, which is a different operation from downloading a game — a tab
  // reading "Downloads (90%)" while nothing is downloading is just wrong, and
  // it also overran the pill. Sync progress belongs to the Library Sync
  // section on the Settings tab, which already reports it.
  const downloadsLabel = t("tabs.downloads");

  // Titles are components (a focus-driven marquee), so Steam must render
  // them as-is: without `bTitleAlreadyLocalized` it runs the title through
  // its string localizer, which expects a string.
  return (
    <div
      ref={rootRef}
      className={ROOT_CLASS}
      style={{ position: "relative", height }}
    >
      {TABS_CSS && <style>{TABS_CSS}</style>}
      <Tabs
        activeTab={tab}
        onShowTab={(next: string) => setTab(next as ActiveTab)}
        tabs={[
          {
            id: "settings",
            title: <QamTabLabel text={t("tabs.settings")} />,
            bTitleAlreadyLocalized: true,
            content: (
              <>
                <StoreConnections />
                <LibrarySync />
                <LanguageSelector />
                <GameDetailsViewModeToggle />
                <CollectionsToggle />
                <StoreOwnershipToggle />
                <PluginUpdater />
                <CleanupSection />
                <CaptureLogsSection />
              </>
            ),
          },
          {
            id: "downloads",
            title: <QamTabLabel text={downloadsLabel} />,
            bTitleAlreadyLocalized: true,
            content: <DownloadsTab />,
          },
        ]}
      />
    </div>
  );
};
