/**
 * LunaIcon: brand glyph for Amazon Luna.
 *
 * The Luna mark is a rounded triangle outline with a node at each corner: a
 * rounded knob at the apex, a small node bottom-left and a large one
 * bottom-right. Amazon ships it only as raster (the site's
 * `brand/Round_Icon_128x128.png`) and react-icons has no Luna glyph, so the
 * geometry was measured off that PNG (pixel space of the 128px icon, fitted
 * to its white mask at IoU 0.91) and is drawn in that space; the viewBox
 * crops to the mark.
 *
 * Fills only, one flat `currentColor`, like the react-icons glyphs this sits
 * beside in `<StoreIcon>`. The outline's sides are filled bands and its
 * rounded corner is a circle rather than a stroke with round joins, because
 * the Store ownership ribbon passes glyphs through an attribute allowlist
 * (`rpc/mixins/_store_ribbon_icons.py`) that has no `stroke-linejoin`.
 */
import { FC, ComponentPropsWithoutRef } from "react";

export interface LunaIconProps extends ComponentPropsWithoutRef<"svg"> {
  size?: number | string;
  color?: string;
}

export const LunaIcon: FC<LunaIconProps> = ({
  size = "1em",
  color,
  style,
  ...props
}) => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    viewBox="32 32 64 64"
    width={size}
    height={size}
    fill="currentColor"
    style={{ color, ...style }}
    {...props}
  >
    {/* Triangle outline: left, bottom and right sides. */}
    <polygon points="60.92,43.41 33.12,84.86 37.28,87.64 65.08,46.19" />
    <polygon points="35.26,88.75 87.86,87.5 87.74,82.5 35.14,83.75" />
    <polygon points="89.93,83.69 65.13,43.49 60.87,46.11 85.67,86.31" />
    {/* Rounded bottom-left corner of the outline. */}
    <circle cx="35.2" cy="86.25" r="2.5" />
    {/* Apex knob and the two corner nodes. */}
    <rect x="54" y="34" width="16.75" height="21" rx="7" />
    <circle cx="42" cy="82.5" r="8" />
    <circle cx="84.25" cy="83" r="11" />
  </svg>
);
