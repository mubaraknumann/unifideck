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

/**
 * Supported store icons
 */
const STORE_ICONS: Record<StoreFinal, React.ComponentType<{ size?: string }>> = {
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
}: {
  store: StoreFinal;
  size?: string;
}) => {
  const IconComponent = STORE_ICONS[store];

  if (!IconComponent) {
    return <FaGamepad size={size} />;
  }

  return <IconComponent size={size} />;
};

export default StoreIcon;
