/**
 * Steam's tabbed section under the play bar, for our games.
 *
 * App-Details builds a plain array of tabs
 * (`{id, title, content, renderTabAddon?, feature?}`: WhatsNew, YourStuff,
 * Community, GameInfo) and hands it to the tabbed-page component, an
 * exported `React.memo` (module source holds `.TabRowTabs` and
 * `activeTab:`, the same anchor `@decky/ui`'s `Tabs` uses). Wrapping the
 * memo's `type` lets us rewrite `props.tabs` before Steam renders them, so
 * our tab is a real native tab: same header, focus, route
 * (`/library/app/:id/tab/:tab`) and transitions.
 *
 * The memo is shared by every tabbed page in Steam. Ours are recognised by
 * the GameInfo tab, whose content element carries the app's overview.
 *
 * React caches a memo's inner function on a fiber when it mounts, so this
 * is installed at boot, before any game page is open.
 */
import { isUnifideckAppId } from "./gate";
import { patchSlot } from "./slot-patch";
import { findExportInModule, isMemo, type MemoObject } from "./webpack";

export interface NativeTab {
  id: string;
  title: unknown;
  content: unknown;
  renderTabAddon?: () => unknown;
  feature?: unknown;
}

export interface TabbedPageProps {
  tabs?: NativeTab[];
  activeTab?: string;
  [key: string]: unknown;
}

export type TabsDecorator = (
  props: TabbedPageProps,
  overview: { appid: number },
) => TabbedPageProps;

/** The overview inside Steam's GameInfo tab content, if this is an
 *  App-Details tab list. */
export function appOverviewOfTabs(tabs: unknown): { appid: number } | null {
  if (!Array.isArray(tabs)) return null;
  const info = tabs.find((t) => (t as NativeTab | null)?.id === "GameInfo") as
    | NativeTab
    | undefined;
  const overview = (
    info?.content as
      | { props?: { children?: { props?: { overview?: unknown } } } }
      | undefined
  )?.props?.children?.props?.overview as { appid?: unknown } | undefined;
  return overview && typeof overview.appid === "number"
    ? (overview as { appid: number })
    : null;
}

export function patchTabbedPage(decorate: TabsDecorator): (() => void) | null {
  const memo = findExportInModule([".TabRowTabs", "activeTab:"], isMemo);
  if (!memo) return null;
  return patchSlot<MemoObject["type"]>(
    memo,
    "type",
    (original) =>
      function tabbedPage(this: unknown, props, ref) {
        let next = props;
        try {
          const overview = appOverviewOfTabs(
            (props as TabbedPageProps | undefined)?.tabs,
          );
          if (overview && isUnifideckAppId(overview.appid)) {
            next = decorate(props as TabbedPageProps, overview);
          }
        } catch (e) {
          console.error("[Unifideck NativeDetails] tabs left stock:", e);
        }
        return original.call(this, next, ref);
      },
  );
}
