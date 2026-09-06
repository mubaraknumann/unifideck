/**
 * Store identifiers and store-specific configuration types.
 *
 * Kept separate from `api.ts` so consumers that only need
 * the union (e.g. icon component, store filter dropdown)
 * don't pull in the full DTO surface.
 */
import type { StoreId } from "./api";
export type { StoreId };
/** Full set of stores the architecture knows about, including
 *  not-yet-implemented ones flagged in the design doc. */
export type StoreIdExtended = StoreId | "ea" | "itch";

/** Per-store visual config used by `<StoreIcon>`. */
export interface StoreVisual {
  id: StoreId;
  display_name: string;
  brand_color: string; // hex with #
  icon_path: string; // relative to /assets
}

/**
 * Visual configuration for each store : icon, brand colour,
 * display name. Single source of truth — components must
 * read from here rather than hard-coding store-specific
 * branding.
 */
export const STORE_VISUALS: Record<StoreId, StoreVisual> = {
  steam: {
    id: "steam",
    display_name: "Steam",
    brand_color: "#1b2838",
    icon_path: "/assets/steam.svg",
  },
  epic: {
    id: "epic",
    display_name: "Epic Games",
    brand_color: "#000000",
    icon_path: "/assets/epic.svg",
  },
  gog: {
    id: "gog",
    display_name: "GOG",
    brand_color: "#86328a",
    icon_path: "/assets/gog.svg",
  },
  amazon: {
    id: "amazon",
    display_name: "Amazon Games",
    brand_color: "#ff9900",
    icon_path: "/assets/amazon.svg",
  },
  microsoft: {
    id: "microsoft",
    display_name: "Xbox",
    brand_color: "#107c10",
    icon_path: "/assets/microsoft.svg",
  },
  ubisoft: {
    id: "ubisoft",
    display_name: "Ubisoft Connect",
    brand_color: "#0052cc",
    icon_path: "/assets/ubisoft.svg",
  },
  battlenet: {
    id: "battlenet",
    display_name: "Battle.net",
    brand_color: "#00aeff",
    icon_path: "/assets/battlenet.svg",
  },
  gamevault: {
    id: "gamevault",
    display_name: "GameVault",
    brand_color: "#1a9c3e",
    icon_path: "/assets/gamevault.svg",
  },
};
