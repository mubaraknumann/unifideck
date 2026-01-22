import {
  SiAmazongames,
  SiEa,
  SiEpicgames,
  SiGogdotcom,
  SiUbisoft,
  SiBattledotnet,
  SiItchdotio,
} from "react-icons/si";
import { FaGamepad } from "react-icons/fa";
import { StoreFinal } from "../types/store";
import type { IconType } from "react-icons";

/**
 * Supported store icons
 */
const STORE_ICONS: Record<StoreFinal, IconType> = {
  epic: SiEpicgames,
  gog: SiGogdotcom,
  amazon: SiAmazongames,
  ubisoft: SiUbisoft,
  ea: SiEa,
  battlenet: SiBattledotnet,
  itch: SiItchdotio,
};

/**
 * Store icon based on store type
 */
const StoreIcon = ({
  store,
  size = "18px",
  color = "inherit",
}: {
  store: StoreFinal;
  size?: string;
  color?: string;
}) => {
  const IconComponent = STORE_ICONS[store];

  if (!IconComponent) {
    return <FaGamepad size={size} color={color} />;
  }

  return <IconComponent size={size} color={color} />;
};

export default StoreIcon;
