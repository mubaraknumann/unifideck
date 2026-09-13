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
 */
import { FC, useLayoutEffect, useRef, useState } from "react";
import { Tabs } from "@decky/ui";
import { useTranslation } from "react-i18next";
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

/** The two Quick-Access tabs. */
type ActiveTab = "settings" | "downloads";

/** Last-viewed tab persisted across QAM mount/unmount. */
let persistentActiveTab: ActiveTab = "settings";

/** Decky's Quick-Access content slot for the active plugin tab. */
const QUICKACCESS_SLOT_SELECTOR = '[id^="quickaccess_content_"]';

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
  // subtract our sibling (Decky's title bar) from it and re-measure on
  // resize, which also covers the Deck/desktop QAM size difference.
  useLayoutEffect(() => {
    const root = rootRef.current;
    const slot = root?.closest<HTMLElement>(QUICKACCESS_SLOT_SELECTOR);
    if (!root || !slot) return;
    const measure = (): void => {
      const titleBarHeight =
        root.previousElementSibling?.getBoundingClientRect().height ?? 0;
      setHeight(slot.clientHeight - titleBarHeight);
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

  return (
    <div ref={rootRef} style={{ position: "relative", height }}>
      <Tabs
        activeTab={tab}
        onShowTab={(next: string) => setTab(next as ActiveTab)}
        tabs={[
          {
            id: "settings",
            title: t("tabs.settings"),
            content: (
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
            ),
          },
          {
            id: "downloads",
            title: downloadsLabel,
            content: <DownloadsTab />,
          },
        ]}
      />
    </div>
  );
};
