import { afterEach, describe, expect, it } from "vitest";
import { steamText } from "./steam-parts";

// The suite runs in node; Steam's page has a `window`.
const g = globalThis as unknown as { window?: unknown };
g.window ??= globalThis;
const w = g.window as { LocalizationManager?: unknown };

describe("steamText", () => {
  afterEach(() => {
    delete w.LocalizationManager;
  });

  it("fills Steam's %N$s slots", () => {
    w.LocalizationManager = {
      LocalizeString: (t: string) =>
        t === "#AppDetails_PlayerUnlockedPercent" ? "You've unlocked %1$s/%2$s" : t,
    };
    expect(steamText("#AppDetails_PlayerUnlockedPercent", "x", 3, 44)).toBe("You've unlocked 3/44");
  });

  it("falls back when Steam has no such token or no localizer", () => {
    w.LocalizationManager = { LocalizeString: (t: string) => t };
    expect(steamText("#Missing", "fallback")).toBe("fallback");
    delete w.LocalizationManager;
    expect(steamText("#Anything", "fallback")).toBe("fallback");
  });
});
