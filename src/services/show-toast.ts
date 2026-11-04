/**
 * show-toast — the one path every toast takes to the screen.
 *
 * Every field is clamped to the toast budget (`toast-text.ts`). ESLint bans
 * `toaster.toast` anywhere but here.
 *
 * The one exception is the achievement card (`showAchievementToast`): every
 * field on it is a name the store sent (achievement, game, store), not text
 * we translate, so it is left to the toast template's own one-line ellipsis,
 * the way Steam's achievement popup cuts a long name.
 */
import { toaster } from "@decky/api";
import { createElement, type ReactNode } from "react";
import { getAchievementIcon } from "../lib/steam-bridge/native-app-details";
import { StoreIcon } from "../components/shared/StoreIcon";
import { STORE_VISUALS, type StoreId } from "../types/store";
import {
  clampToastField,
  type ToastString,
  type ToastHeader,
} from "./toast-text";

export interface ToastOptions {
  title: ToastHeader;
  body?: ToastString;
  /** The action's label, shown under the body when the toast is clickable. */
  subtext?: ToastString;
  duration: number;
  onClick?: () => void;
  /** Leave the body to the toast template's own one-line ellipsis instead
   *  of the budget. Only for an instruction that must be read whole; its
   *  key is listed in `FULL_BODY_KEYS` in `scripts/check_toast_length.py`. */
  fullBody?: boolean;
}

/** Show a toast, every field clamped to the toast budget. */
export function showToast(options: ToastOptions): void {
  const title = clampToastField(options.title);
  const rawBody = options.body ?? "";
  const body = options.fullBody ? rawBody : rawBody && clampToastField(rawBody);
  const subtext = options.subtext ? clampToastField(options.subtext) : "";
  try {
    toaster.toast({
      title,
      body,
      duration: options.duration,
      ...(options.onClick ? { onClick: options.onClick } : {}),
      ...(subtext ? { subtext } : {}),
    });
  } catch {
    console.log(`[Toast] ${title}: ${body}`);
  }
}

/** A name from a store, bounded so a hostile value cannot flood a toast. */
const NAME_LIMIT = 120;

function bounded(text: string): string {
  return text.length > NAME_LIMIT ? `${text.slice(0, NAME_LIMIT - 1)}…` : text;
}

export interface AchievementToastOptions {
  /** The achievement's name, as the store sent it. */
  name: string;
  /** The achievement's image URL; the store logo stands in when empty. */
  icon?: string;
  store: string;
  gameTitle?: string;
  /** Share of players with it; under 10% draws Steam's rare glow. */
  rarity?: number | null;
  duration: number;
}

/** The popup's image: Steam's own achievement icon, so a rare one glows. */
function achievementLogo(options: AchievementToastOptions): ReactNode {
  const store = options.store as StoreId;
  if (!options.icon) {
    return createElement(StoreIcon, { store, size: "100%", color: "#fff" });
  }
  const SteamIcon = getAchievementIcon();
  if (SteamIcon) {
    // As Steam's own achievement popup: the glow is drawn, not spun.
    return createElement(SteamIcon, {
      achievement: {
        name: options.name,
        hidden: false,
        icon_achieved: options.icon,
        icon_unachieved: options.icon,
      },
      unlocked: true,
      globalUnlockPercentage: options.rarity ?? undefined,
      pauseAnimation: true,
      size: "fill",
    });
  }
  return createElement("img", {
    src: options.icon,
    alt: "",
    style: { width: "100%", height: "100%", objectFit: "cover" },
  });
}

/**
 * A Steam-style achievement popup: the achievement's image on the left
 * (with Steam's glow when it is rare), the store logo and the game as the
 * header, and the achievement's name underneath.
 */
export function showAchievementToast(options: AchievementToastOptions): void {
  const store = options.store as StoreId;
  const storeName = STORE_VISUALS[store]?.display_name ?? options.store;
  // The store logo beside it already names the store; its name is only
  // the header when there is no game title.
  const header = options.gameTitle ? bounded(options.gameTitle) : storeName;
  try {
    toaster.toast({
      title: header,
      body: bounded(options.name),
      logo: achievementLogo(options),
      icon: createElement(StoreIcon, { store, size: 16, color: "#fff" }), // i18n-ignore: an icon colour, not text
      duration: options.duration,
    });
  } catch {
    console.log(`[Toast] achievement: ${options.name} (${header})`);
  }
}
