/**
 * The colour-coded compatibility pill.
 *
 * One definition shared by {@link GameInfoCompatRow} and
 * {@link GameInfoDetailsModal}, which each carried their own copy of
 * the colour and label maps.
 *
 * Labels are named after the device the rating describes: a Steam
 * Machine owner reads "Steam Machine Compatibility", not the Deck's.
 */
import { FC } from "react";
import { useTranslation } from "react-i18next";
import type { CompatTrack } from "../../lib/steam-bridge/compat-packed";

export type CompatCategory = 0 | 1 | 2 | 3;

export const COMPAT_COLORS: Record<CompatCategory, { bg: string; fg: string }> =
  {
    3: { bg: "#59bf40", fg: "#ffffff" },
    2: { bg: "#ffc82c", fg: "#000000" },
    1: { bg: "#ff4444", fg: "#ffffff" },
    0: { bg: "#666666", fg: "#ffffff" },
  };

/**
 * Category → i18n key suffix.
 *
 * The SteamOS track's middle rung is "compatible" rather than
 * "playable" — Valve words it that way because it answers "does this
 * run on SteamOS", not "how well does it run on this device".
 */
const LABEL_SUFFIX: Record<CompatCategory, string> = {
  3: "verified",
  2: "playable",
  1: "unsupported",
  0: "unknown",
};

/** i18n key for the compat label, given the track and category. */
export function compatLabelKey(
  category: CompatCategory,
  track: CompatTrack,
): string {
  const suffix =
    track === "steamos" && category === 2
      ? "compatible"
      : LABEL_SUFFIX[category];
  return `gameInfoPanel.compatibility.${suffix}`;
}

/** i18n key for the "<device> Compatibility" heading. */
export function compatTitleKey(track: CompatTrack): string {
  switch (track) {
    case "machine":
      return "gameInfoPanel.compatibility.modalTitleMachine";
    case "steamos":
      return "gameInfoPanel.compatibility.modalTitleSteamOS";
    default:
      return "gameInfoPanel.compatibility.modalTitleDeck";
  }
}

/** Normalise an arbitrary number to a known category. */
export function asCategory(value: unknown): CompatCategory {
  return value === 1 || value === 2 || value === 3 ? value : 0;
}

interface Props {
  category: CompatCategory;
  track: CompatTrack;
  style?: React.CSSProperties;
}

export const CompatBadge: FC<Props> = ({ category, track, style }) => {
  const { t } = useTranslation();
  const colors = COMPAT_COLORS[category];
  return (
    <span
      style={{
        background: colors.bg,
        color: colors.fg,
        padding: "2px 8px",
        borderRadius: 4,
        fontSize: 12,
        fontWeight: 600,
        textTransform: "uppercase",
        ...style,
      }}
    >
      {t(compatLabelKey(category, track))}
    </span>
  );
};
