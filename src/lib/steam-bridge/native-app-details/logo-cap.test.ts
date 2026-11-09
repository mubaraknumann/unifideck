import { describe, expect, it } from "vitest";
import { capLogoPosition, LOGO_CORNER, MAX_LOGO_HEIGHT_PCT, MAX_LOGO_WIDTH_PCT } from "./logo-cap";

describe("capLogoPosition", () => {
  it("pins to bottom-left and caps an oversized box", () => {
    expect(
      capLogoPosition({ pinnedPosition: "CenterCenter", nWidthPct: 97, nHeightPct: 81 }),
    ).toEqual({
      pinnedPosition: LOGO_CORNER,
      nWidthPct: MAX_LOGO_WIDTH_PCT,
      nHeightPct: MAX_LOGO_HEIGHT_PCT,
    });
  });

  it("caps each side on its own", () => {
    expect(
      capLogoPosition({ pinnedPosition: "BottomLeft", nWidthPct: 47, nHeightPct: 100 }),
    ).toEqual({
      pinnedPosition: LOGO_CORNER,
      nWidthPct: 47,
      nHeightPct: MAX_LOGO_HEIGHT_PCT,
    });
  });

  it("keeps a small box's size but still moves it bottom-left", () => {
    expect(capLogoPosition({ pinnedPosition: "TopLeft", nWidthPct: 30, nHeightPct: 20 })).toEqual({
      pinnedPosition: LOGO_CORNER,
      nWidthPct: 30,
      nHeightPct: 20,
    });
  });
});
