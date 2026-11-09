/**
 * The limits on a logo box we borrow from a Steam game (artwork-patch.ts).
 *
 * Steam does not cap the box: it only rejects values outside 0-100, and
 * GTA IV's own box is 47% x 100% of the hero. The logo is scaled to fill
 * the box. That box was sized for Steam's logo image; ours is often a
 * tighter crop of a different image and would fill far more of the hero.
 *
 * So only the size is borrowed, capped at 50% x 50% (Steam's own default
 * box for art with no stored position), and the logo always sits
 * bottom-left, where Steam puts a shortcut's logo. A capped logo is never
 * larger than Steam draws a shortcut's logo, and never covers the middle
 * of the art.
 */

/** Steam's logo placement: pinned corner and box size, % of the hero. */
export interface LogoPosition {
  pinnedPosition: string;
  nWidthPct: number;
  nHeightPct: number;
}

/** Steam's name for the bottom-left corner (its default for shortcuts). */
export const LOGO_CORNER = "BottomLeft";
export const MAX_LOGO_WIDTH_PCT = 50;
export const MAX_LOGO_HEIGHT_PCT = 50;

/** The position pinned bottom-left, its box capped to MAX_LOGO_*_PCT. */
export function capLogoPosition(position: LogoPosition): LogoPosition {
  return {
    ...position,
    pinnedPosition: LOGO_CORNER,
    nWidthPct: Math.min(position.nWidthPct, MAX_LOGO_WIDTH_PCT),
    nHeightPct: Math.min(position.nHeightPct, MAX_LOGO_HEIGHT_PCT),
  };
}
