/**
 * `clampToastField` — the render-time half of the toast budget.
 *
 * Translations are held to the budget by `scripts/check_toast_length.py`;
 * this clamp covers what only exists at runtime, an interpolated game
 * title. Same width model as the gate.
 */
import { describe, expect, it, vi } from "vitest";

const STRINGS: Record<string, string> = {
  "toasts.launcher.attemptingInstall": "Getting {{version}}",
  "toasts.header.store": "{{storeName}}",
  "toasts.test.tooLong": "This wording is far too long {{title}}",
};

vi.mock("i18next", () => ({
  default: {
    language: "en-US",
    t: (key: string, params?: Record<string, string | number>) =>
      (STRINGS[key] ?? key).replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name: string) =>
        String(params?.[name] ?? ""),
      ),
  },
}));

const { clampToastField, toastText, headerText, backendToastText } = await import("./toast-text");

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

describe("translated toast text", () => {
  it("never cuts an interpolated value", () => {
    expect(toastText("toasts.launcher.attemptingInstall", { version: "GE-Proton10-25" })).toBe(
      "Getting GE-Proton10-25",
    );
  });

  it("never cuts a value in a header", () => {
    const storeName = "A Very Long Store Name Indeed";
    expect(headerText("toasts.header.store", { storeName })).toBe(storeName);
  });

  it("never cuts a value the backend sent", () => {
    expect(
      backendToastText("toasts.launcher.attemptingInstall", { version: "GE-Proton10-25" }),
    ).toBe("Getting GE-Proton10-25");
  });

  it("still holds our own wording to the budget", () => {
    expect(toastText("toasts.test.tooLong")).toBe("This wording is…");
  });
});
