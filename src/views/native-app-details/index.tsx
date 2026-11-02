/**
 * Native App-Details: Steam renders the game page for our shortcuts.
 *
 * Instead of hiding Steam's play bar and tabs and drawing a copy, we steer
 * Steam's own components with our data and our actions:
 *
 *  - Action button: Steam's, label from our state, click to our handlers.
 *  - Play bar row: Steam's, with our stats and status line in its middle
 *    our Uninstall button beside Steam's controller and gear buttons, and
 *    our cloud-save status line under it (as Steam's Steam Cloud line).
 *  - Tabs: Steam's Your stuff (the default), then our Game info tab.
 *    Activity and Community are dropped: Steam has nothing for a shortcut.
 *  - Uninstall: in Steam's gear menu (app-context-menu-patch.ts).
 *
 * If Steam's action button or tabbed page cannot be found on this build,
 * nothing is patched and `AppDetailsPatch` keeps the legacy custom UI.
 * Findings and evidence: docs/feasibility/native-app-details.md.
 */
import {
  cloneElement,
  isValidElement,
  type ReactElement,
  type ReactNode,
} from "react";
import {
  getNativeClasses,
  patchActionButton,
  patchTabbedPage,
  setActionRowDecorator,
  setPlaySectionDecorator,
  isPlaySectionPatched,
  setNativeAppDetailsActive,
  unpatchActionRow,
  type ActionRowProps,
  type NativeTab,
  type TabbedPageProps,
} from "../../lib/steam-bridge/native-app-details";
import { NativeActionHost } from "./NativeActionHost";
import { NativeAppButtons } from "./NativeAppButtons";
import { NativeCloudStatusRow } from "./NativeCloudStatusRow";
import { NativeGameInfoTab } from "./NativeGameInfoTab";
import { NativeAchievements } from "./NativeAchievements";
import { HeaderLatchNudge } from "./HeaderLatchNudge";
import { GameInfoTabAddon } from "./GameInfoTabAddon";
import { NativeStatusAndStats } from "./NativeStatusAndStats";

const LOG = "[Unifideck NativeDetails]";

function childList(children: ReactNode): ReactNode[] {
  if (Array.isArray(children)) return children;
  return children == null ? [] : [children];
}

/** Swap the stats element for ours and add our buttons to AppButtons. */
function decorateRow(
  row: ReactElement,
  _props: ActionRowProps,
  appId: number,
): ReactElement {
  const appButtons = getNativeClasses().details.AppButtons;
  const kids = childList((row.props as { children?: ReactNode }).children).map(
    (child) => {
      if (!isValidElement(child)) return child;
      const p = child.props as {
        statusPanelType?: unknown;
        overview?: object;
        className?: unknown;
        children?: ReactNode;
      };
      if (p.statusPanelType !== undefined && p.overview) {
        return (
          <NativeStatusAndStats
            key="unifideck-status-and-stats"
            appId={appId}
            overview={p.overview}
          />
        );
      }
      if (
        appButtons &&
        typeof p.className === "string" &&
        p.className.includes(appButtons)
      ) {
        return cloneElement(
          child,
          undefined,
          ...childList(p.children),
          <NativeAppButtons key="unifideck-app-buttons" appId={appId} />,
        );
      }
      return child;
    },
  );
  const decorated = cloneElement(row, undefined, ...kids);
  // The cloud-save line belongs after the whole PlaySection, where Steam
  // puts its own "STEAM CLOUD" line (decoratePlaySection). Only when that
  // patch is not live does it go here, under the row.
  if (isPlaySectionPatched()) return decorated;
  return (
    <>
      {decorated}
      <NativeCloudStatusRow appId={appId} />
    </>
  );
}

/** Our cloud-save line right after Steam's PlaySection, as Steam's own. */
function decoratePlaySection(
  section: ReactElement,
  _props: ActionRowProps,
  appId: number,
): ReactElement {
  return (
    <>
      {section}
      <NativeCloudStatusRow appId={appId} />
    </>
  );
}

const tabsCache = new WeakMap<NativeTab[], NativeTab[]>();

/** Steam's Your stuff (with our achievements on top), the tab the page
 *  opens on, then our Game info. */
function decorateTabs(
  props: TabbedPageProps,
  overview: { appid: number },
): TabbedPageProps {
  const original = props.tabs ?? [];
  let tabs = tabsCache.get(original);
  if (!tabs) {
    const info = original.find((t) => t.id === "GameInfo");
    const yourStuff = original.find((t) => t.id === "YourStuff");
    if (!info) return props;
    const appId = overview.appid;
    const body = <NativeGameInfoTab appId={appId} overview={overview} />;
    const ours: NativeTab = {
      ...info,
      // Steam's compat icon beside the title, from our category.
      renderTabAddon: () => <GameInfoTabAddon appId={appId} />,
      // Keep Steam's content shell (section wrapper, error boundary).
      content: isValidElement(info.content)
        ? cloneElement(info.content, undefined, body)
        : body,
    };
    // Your stuff stays Steam's (media, notes); our achievements go on top,
    // where Steam puts its own.
    const stuff: NativeTab | undefined =
      yourStuff && isValidElement(yourStuff.content)
        ? {
            ...yourStuff,
            content: cloneElement(
              yourStuff.content,
              undefined,
              <HeaderLatchNudge key="unifideck-header-nudge" />,
              <NativeAchievements key="unifideck-achievements" appId={appId} />,
              (yourStuff.content.props as { children?: ReactNode }).children,
            ),
          }
        : yourStuff;
    // Your stuff first, then Game info (user's order). Your stuff is also
    // the tab the page opens on (see activeTab below).
    tabs = stuff ? [stuff, ours] : [ours];
    tabsCache.set(original, tabs);
  }
  // Open on the first tab (Your stuff), unless the route names one of ours.
  const activeTab = tabs.some((t) => t.id === props.activeTab)
    ? props.activeTab
    : tabs[0].id;
  return { ...props, tabs, activeTab };
}

/**
 * Install every native App-Details patch. Returns the disposer, or `null`
 * when a required seam is missing (the legacy UI then stays in charge).
 */
export function startNativeAppDetails(): (() => void) | null {
  let unpatchButton: (() => void) | null = null;
  let unpatchTabs: (() => void) | null = null;
  try {
    unpatchButton = patchActionButton((element, appId) => (
      <NativeActionHost
        element={element as ReactElement<{ overview?: object }>}
        appId={appId}
      />
    ));
    if (!unpatchButton) {
      console.warn(
        `${LOG} Steam's action button not found; using the legacy game page`,
      );
      return null;
    }
    unpatchTabs = patchTabbedPage(decorateTabs);
    if (!unpatchTabs) {
      console.warn(
        `${LOG} Steam's tabbed page not found; using the legacy game page`,
      );
      unpatchButton();
      return null;
    }
  } catch (e) {
    console.error(`${LOG} install failed; using the legacy game page:`, e);
    unpatchTabs?.();
    unpatchButton?.();
    return null;
  }
  setActionRowDecorator(decorateRow);
  setPlaySectionDecorator(decoratePlaySection);
  setNativeAppDetailsActive(true);
  console.log(`${LOG} active`);
  const button = unpatchButton;
  const tabs = unpatchTabs;
  return () => {
    setNativeAppDetailsActive(false);
    setActionRowDecorator(null);
    setPlaySectionDecorator(null);
    unpatchActionRow();
    tabs();
    button();
  };
}
