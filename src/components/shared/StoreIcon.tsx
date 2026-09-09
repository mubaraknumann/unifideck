/**
 * StoreIcon — brand badge for a store.
 *
 * Vector-icon based (react-icons) — staging used the same set so the
 * visual matches across the codebase. Falls back to a generic
 * `FaGamepad` glyph for unknown stores so the layout never breaks.
 */
import { FC } from "react";
import {
  SiAmazongames,
  SiEpicgames,
  SiGogdotcom,
  SiBattledotnet,
  SiUbisoft,
} from "react-icons/si";
import { FaGamepad, FaSteam, FaXbox } from "react-icons/fa";
import { GameVaultIcon } from "./GameVaultIcon";
import type { StoreId } from "../../types/api";

/**
 * The map holds two kinds of component: react-icons glyphs (`IconType`) and
 * our own `GameVaultIcon`. Both accept the only two props StoreIcon passes,
 * so the map is typed to that narrower shape rather than casting one of them
 * through `unknown` to satisfy `IconType`.
 */
type StoreGlyph = FC<{ size?: number | string; color?: string }>;

const STORE_ICONS: Record<StoreId, StoreGlyph> = {
  steam: FaSteam,
  epic: SiEpicgames,
  gog: SiGogdotcom,
  amazon: SiAmazongames,
  microsoft: FaXbox,
  ubisoft: SiUbisoft,
  battlenet: SiBattledotnet,
  gamevault: GameVaultIcon,
};

interface Props {
  store: StoreId;
  size?: number | string;
  color?: string;
}

export const StoreIcon: FC<Props> = ({
  store,
  size = 16,
  color = "inherit",
}) => {
  const Icon = STORE_ICONS[store] ?? FaGamepad;
  return <Icon size={size} color={color} />;
};
