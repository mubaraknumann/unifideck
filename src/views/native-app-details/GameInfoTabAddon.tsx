/**
 * GameInfoTabAddon: Steam's compat icon beside the GAME INFO tab title.
 *
 * Steam draws it for Steam games from the overview; for a shortcut it draws
 * nothing. This draws the same icon from the category our backend resolved
 * for this device's track. Nothing until our metadata has loaded.
 */
import { FC } from "react";
import { useGameMetadata } from "../../hooks/useGameMetadata";
import { asCategory } from "../../components/info/CompatBadge";
import { CompatTabAddon } from "./NativeCompat";

export const GameInfoTabAddon: FC<{ appId: number }> = ({ appId }) => {
  const { data: meta } = useGameMetadata(appId);
  if (!meta) return null;
  const track = meta.compat_device;
  const category = asCategory(meta.compat?.[track]?.category);
  return <CompatTabAddon track={track} category={category} />;
};
