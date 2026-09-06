/**
 * `buildToastParams` — the interpolation contract for backend toasts.
 *
 * The cloud-sync failure strings interpolate `{{error}}`, but the backend
 * deliberately sends a machine **code** plus the key that translates it
 * (`error_i18n_key: "cloudSync.error.disk_full"`) rather than English, which
 * would be untranslatable. Nothing resolved that key.
 *
 * That mattered the moment the cloud-failure reporter was wired up: its whole
 * module had been unimported (audit register item 37), so its payload had
 * never been rendered by anything and the gap was invisible. A real upload
 * failure would have read *"Cloud save upload failed for gog (). Your progress
 * is stored locally only."* — audit §2.9's lesson that wiring dead code is a
 * behaviour change needing the same scrutiny as a fix.
 */
import { describe, expect, it, vi } from "vitest";

vi.mock("i18next", () => ({
  default: {
    t: (key: string) => (key === "cloudSync.error.disk_full" ? "Not enough disk space" : key),
  },
}));

const { buildToastParams } = await import("./toast-params");

describe("buildToastParams", () => {
  it("resolves the error code into the {{error}} placeholder", () => {
    const params = buildToastParams({
      i18n_params: {
        store: "gog",
        error_code: "disk_full",
        error_i18n_key: "cloudSync.error.disk_full",
      },
    });
    expect(params.error).toBe("Not enough disk space");
    expect(params.store).toBe("gog");
  });

  it("falls back to the raw code when the key has no translation", () => {
    // An untranslated code still tells the user, and a bug report, more
    // than a blank pair of brackets.
    const params = buildToastParams({
      i18n_params: {
        error_code: "some_new_code",
        error_i18n_key: "cloudSync.error.some_new_code",
      },
    });
    expect(params.error).toBe("some_new_code");
  });

  it("never overwrites an explicit error param", () => {
    const params = buildToastParams({
      i18n_params: {
        error: "already set",
        error_i18n_key: "cloudSync.error.disk_full",
      },
    });
    expect(params.error).toBe("already set");
  });

  it("merges game_title in as gameTitle", () => {
    // Without this every launcher toast rendered the placeholder unfilled:
    // "Starting  through Battle.net…".
    const params = buildToastParams({ game_title: "The Witcher" });
    expect(params.gameTitle).toBe("The Witcher");
  });

  it("lets an explicit i18n_params entry win over game_title", () => {
    const params = buildToastParams({
      game_title: "top level",
      i18n_params: { gameTitle: "explicit" },
    });
    expect(params.gameTitle).toBe("explicit");
  });

  it("handles a payload with neither field", () => {
    expect(buildToastParams({})).toEqual({});
  });

  it("ignores a non-string error_i18n_key", () => {
    const params = buildToastParams({
      i18n_params: { error_i18n_key: 42 as unknown as string },
    });
    expect(params.error).toBeUndefined();
  });
});
