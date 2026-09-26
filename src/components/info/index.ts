/**
 * Info panel — barrel export.
 *
 * Mirrors staging's three-row Game Info Panel layout, split into
 * focused sub-components: a compat / actions row, an inline
 * info card, a synopsis block (gated on the toggle), and a row
 * of Steam navigation buttons. The container in
 * {@link ./GameInfoPanel} reads {@link useGameInfo} +
 * {@link useGameMetadata} and forwards data into each section.
 */
export { GameInfoPanel } from "./GameInfoPanel";
export { GameInfoCompatRow } from "./GameInfoCompatRow";
export { GameInfoInfoRow } from "./GameInfoInfoRow";
export { GameInfoSynopsisSection } from "./GameInfoSynopsisSection";
export { GameInfoNavButtons } from "./GameInfoNavButtons";
export { GameInfoDetailsModal } from "./GameInfoDetailsModal";
export { GameStoreSwitcher } from "./GameStoreSwitcher";
