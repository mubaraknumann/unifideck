import { describe, expect, it, vi } from "vitest";

vi.mock("../api", () => ({}));
vi.mock("../api/event-bus-client", () => ({}));

import { usesXboxSaveLine, xboxSaveLine } from "./useXboxSaveStatus";

describe("xboxSaveLine", () => {
  it("uses Steam's word for in sync", () => {
    expect(xboxSaveLine("in_sync")?.token).toBe("#AppDetails_CloudStatus_Synchronized");
  });

  it.each([
    ["local_newer", "play.cloudSave.statusWaitingUpload"],
    ["cloud_newer", "play.cloudSave.statusWaitingLaunch"],
    ["cloud_removed", "play.cloudSave.statusWaitingLaunch"],
    ["conflict", "play.cloudSave.statusWaitingLaunch"],
    ["running", "play.cloudSave.statusWaitingLaunch"],
    ["unknown", "play.cloudSave.checkConnection"],
    ["offline", "play.cloudSave.checkConnection"],
  ] as const)("%s", (state, key) => {
    expect(xboxSaveLine(state)?.key).toBe(key);
    // Nothing is wrong yet: the runtime settles it at the next launch
    expect(xboxSaveLine(state)?.problem).toBe(false);
  });

  it.each(["no_saves", "not_launched", "", undefined] as const)("says nothing for %s", (state) => {
    expect(xboxSaveLine(state)).toBeNull();
  });
});

describe("usesXboxSaveLine", () => {
  const row = (tags: string[], installed: boolean) =>
    ({ store_tags: tags, is_installed: installed } as never);

  it("is for an installed Xbox PC game", () => {
    expect(usesXboxSaveLine(row(["xbox_pc"], true))).toBe(true);
  });

  it("is not for a stream or a game not installed", () => {
    expect(usesXboxSaveLine(row(["xcloud"], true))).toBe(false);
    expect(usesXboxSaveLine(row(["xbox_pc"], false))).toBe(false);
  });
});
