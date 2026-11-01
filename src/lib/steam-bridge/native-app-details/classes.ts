/**
 * Steam's own CSS-module class names for the App-Details page.
 *
 * Our native-looking pieces (stats, status line, Game info tab) wear these
 * classes so they look exactly like Steam's and so a CSS Loader theme that
 * restyles Steam's page restyles ours with it. Each map is found by a set
 * of readable keys, never by module id or minified name. A key that does
 * not resolve degrades to `""`: that element merely loses Steam's styling.
 */
import { findClassMap } from "./webpack";

type ClassMap = Readonly<Record<string, string>>;

export interface NativeClasses {
  /** Play bar: stats, status line, progress, menu buttons. */
  playBar: ClassMap;
  /** App-Details layout: ActionRow, AppButtons, Game info containers. */
  details: ClassMap;
  /** Game info "about" block: portrait, description, associations. */
  about: ClassMap;
  /** Outer glassy frame around the about block. */
  aboutFrame: ClassMap;
  /** Game info quick links (Store Page, Support, …). */
  links: ClassMap;
  /** One row of the features list (icon + label). */
  feature: ClassMap;
  /** A Your stuff / Activity section: Highlight, Body. */
  section: ClassMap;
  /** Achievements section: progress highlight and carousel items. */
  achievements: ClassMap;
  /** Achievement icon frame. */
  achievementIcon: ClassMap;
  /** The small button at the foot of a section ("Go to my media library"). */
  detailsButton: ClassMap;
}

let cached: NativeClasses | null = null;

export function getNativeClasses(): NativeClasses {
  if (cached) return cached;
  cached = {
    playBar: findClassMap([
      "StatusAndStats",
      "GameStatsSection",
      "PlayBarDetailLabel",
      "DetailsSection",
    ]),
    details: findClassMap([
      "ActionRow",
      "ActionButtonAndStatusPanel",
      "AppButtons",
      "GameInfoQuickLinks",
    ]),
    about: findClassMap([
      "GameDescription",
      "AssociationList",
      "FeaturesList",
      "DeckVerifiedInfo",
    ]),
    aboutFrame: findClassMap(["AppGameInfoContainer", "GameInfoShadow"]),
    links: findClassMap(["LinksSection", "Anchor", "Link", "Text"]),
    feature: findClassMap(["Container", "Icon", "ExtraMargin", "Label"], true),
    section: findClassMap([
      "AppDetailsSectionContainer",
      "Highlight",
      "Body",
      "BodyGutter",
    ]),
    achievements: findClassMap([
      "AchievementCarouselItem",
      "AchivementCarouselItemDetails",
      "HighlightDiv",
      "LockedAchievementsLabel",
    ]),
    achievementIcon: findClassMap([
      "AchievementIconWrapper",
      "RareAchievementNoAnimation",
    ]),
    detailsButton: findClassMap([
      "AppDetailsButton",
      "BottomRight",
      "MoreVisible",
    ]),
  };
  return cached;
}

/** Join class names, dropping the unresolved (empty) ones. */
export function cx(...names: Array<string | false | null | undefined>): string {
  return names.filter(Boolean).join(" ");
}

/** For tests and a Steam update in the same session. */
export function _resetNativeClasses(): void {
  cached = null;
}
