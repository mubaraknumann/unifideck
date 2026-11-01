/**
 * `clampToastField` — the render-time half of the toast budget.
 *
 * Translations are held to the budget by `scripts/check_toast_length.py`;
 * this clamp covers what only exists at runtime, an interpolated game
 * title. Same width model as the gate.
 */
import { describe, expect, it, vi } from "vitest";

vi.mock("i18next", () => ({ default: { language: "en-US", t: (k: string) => k } }));

const { clampToastField } = await import("./toast-text");

describe("clampToastField", () => {
  it("leaves a string that fits alone", () => {
    expect(clampToastField("Download started", "en-US")).toBe("Download started");
  });

  it("cuts the end, so a trailing game title keeps its start", () => {
    expect(clampToastField("Install: Hollow Knight Silksong", "en-US")).toBe("Install: Hollow…");
  });

  it("measures CJK by width, not by character count", () => {
    const fits = "あ".repeat(22);
    expect(clampToastField(fits, "ja-JP")).toBe(fits);
    expect(clampToastField("あ".repeat(23), "ja-JP")).toBe("あ".repeat(21) + "…");
  });

  it("never splits a character from its combining mark", () => {
    const clamped = clampToastField("é".repeat(20), "fr-FR");
    expect(clamped).toBe("é".repeat(15) + "…");
  });
});
