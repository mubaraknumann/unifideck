// @vitest-environment jsdom
/**
 * `specifyNamedCompatTool` never sends an empty tool name: the Steam Deck
 * client of 2026-11 reads `SpecifyCompatTool(appId, "")` as "the global Steam
 * Play default", which put Proton on Unifideck's temporary shortcuts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { specifyNamedCompatTool } from "./shortcut-types";

describe("specifyNamedCompatTool", () => {
  const specify = vi.fn();

  beforeEach(() => {
    (window as unknown as { SteamClient: unknown }).SteamClient = {
      Apps: { SpecifyCompatTool: specify },
    };
  });

  afterEach(() => {
    specify.mockReset();
    delete (window as unknown as { SteamClient?: unknown }).SteamClient;
  });

  it("sets a named tool", () => {
    specifyNamedCompatTool(42, "GE-Proton10-4");
    expect(specify).toHaveBeenCalledWith(42, "GE-Proton10-4");
  });

  it.each([[""], [null], [undefined]])("skips %p", (tool) => {
    specifyNamedCompatTool(42, tool);
    expect(specify).not.toHaveBeenCalled();
  });
});
