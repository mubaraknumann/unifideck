/**
 * Shared atoms — barrel export.
 *
 * Small, reusable pieces used by multiple feature areas :
 *  - StoreIcon  : brand badge for Epic / GOG / Amazon / etc.
 *  - GameGrid   : reusable grid layout for any game list.
 *  - SteamIcons : Steam's own action glyphs, for surfaces that
 *                 sit next to vanilla Steam UI.
 *  - GameVaultIcon : the one store glyph react-icons doesn't ship.
 *
 * Consumers should prefer these over rolling their own
 * markup so a brand-color change or a layout tweak is a
 * one-file edit.
 */
export { StoreIcon } from "./StoreIcon";
export { GameGrid } from "./GameGrid";
export { SteamControllerIcon, SteamGearIcon } from "./SteamIcons";
export { GameVaultIcon } from "./GameVaultIcon";
