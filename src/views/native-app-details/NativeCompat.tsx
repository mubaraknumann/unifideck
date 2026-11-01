/**
 * Compatibility, drawn with Steam's own icon and label for OUR category.
 *
 * Steam's Game info shows "<Device> Compatibility  [icon] VERIFIED  Details"
 * and puts the same icon beside the GAME INFO tab title. For a shortcut
 * Steam renders neither. These do, using Steam's category label and icon
 * helpers and the category our backend resolved for this device's track.
 * If Steam's helpers are missing, the row falls back to our badge and the
 * tab addon to nothing.
 */
import { FC } from "react";
import { Focusable } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { CompatBadge, compatTitleKey } from "../../components/info/CompatBadge";
import {
  cx,
  getCompatParts,
  getNativeClasses,
} from "../../lib/steam-bridge/native-app-details";
import type { CompatTrack } from "../../lib/steam-bridge/compat-packed";

/** Steam's icon component for this track and category, or null. */
function iconFor(track: CompatTrack, category: number) {
  const parts = getCompatParts();
  if (!parts) return null;
  return track === "steamos"
    ? parts.steamOsIcon(category)
    : parts.deckIcon(category);
}

/** Steam's label for this track and category, or null. */
function labelFor(track: CompatTrack, category: number): string | null {
  const parts = getCompatParts();
  if (!parts) return null;
  return track === "steamos"
    ? parts.steamOsLabel(category)
    : parts.deckLabel(category);
}

/** The best rung of each track, which Steam draws inverted on focus. */
function isTopRung(track: CompatTrack, category: number): boolean {
  return track === "steamos" ? category >= 2 : category === 3;
}

/** The icon beside the Game info tab title. */
export const CompatTabAddon: FC<{ track: CompatTrack; category: number }> = ({
  track,
  category,
}) => {
  const Icon = iconFor(track, category);
  if (!Icon) return null;
  const invert = getNativeClasses().details.InvertFocusedIcon;
  return <Icon className={isTopRung(track, category) ? invert : undefined} />;
};

/** The "<Device> Compatibility  [icon] LABEL  Details" row. */
export const NativeCompatRow: FC<{
  track: CompatTrack;
  category: 0 | 1 | 2 | 3;
  onDetails: () => void;
}> = ({ track, category, onDetails }) => {
  const { t } = useTranslation();
  const c = getNativeClasses().about;
  const Icon = iconFor(track, category);
  const label = labelFor(track, category);
  return (
    <Focusable
      className={cx(c.DeckVerifiedInfo)}
      focusClassName={c.Focused}
      flow-children="row"
    >
      <div className={c.Title}>{t(compatTitleKey(track))}</div>
      <div className={c.CompatLabel}>
        {Icon && label ? (
          <>
            <Icon />
            {label}
          </>
        ) : (
          <CompatBadge category={category} track={track} />
        )}
      </div>
      <Focusable
        className={c.Details}
        onActivate={onDetails}
        onClick={onDetails}
      >
        {t("gameInfoPanel.buttons.details")}
      </Focusable>
    </Focusable>
  );
};
