/**
 * `buildToastParams` — the interpolation contract for backend toasts.
 */
import { describe, expect, it } from "vitest";
import { buildToastParams } from "./toast-params";

describe("buildToastParams", () => {
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

  it("passes the other params through", () => {
    const params = buildToastParams({ i18n_params: { store: "gog" } });
    expect(params.store).toBe("gog");
  });

  it("handles a payload with neither field", () => {
    expect(buildToastParams({})).toEqual({});
  });
});
