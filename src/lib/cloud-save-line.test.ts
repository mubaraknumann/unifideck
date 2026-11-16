import { describe, expect, it } from "vitest";
import { CLOUD_LINES, verdictLine } from "./cloud-save-line";

describe("verdictLine", () => {
  it("is Steam's synchronized word for in sync, either model", () => {
    for (const model of ["manual", "runtime"] as const) {
      const line = verdictLine("in_sync", model);
      expect(line.token).toBe("#AppDetails_CloudStatus_Synchronized");
      expect(line.problem).toBe(false);
    }
  });

  it.each([
    ["local_newer", "play.cloudSave.statusWaitingUpload"],
    ["cloud_newer", "play.cloudSave.statusCloudAvailable"],
    ["conflict", "play.cloudSave.statusCloudAvailable"],
  ] as const)("a store we sync shows out of sync for %s", (verdict, key) => {
    const line = verdictLine(verdict, "manual");
    expect(line.token).toBe("#AppDetails_CloudStatus_OutOfSync");
    expect(line.key).toBe(key);
    expect(line.problem).toBe(true);
  });

  it.each([
    ["local_newer", "play.cloudSave.statusWaitingUpload"],
    ["cloud_newer", "play.cloudSave.statusWaitingLaunch"],
    ["conflict", "play.cloudSave.statusWaitingLaunch"],
  ] as const)("a runtime store says what its next launch does for %s", (verdict, key) => {
    const line = verdictLine(verdict, "runtime");
    expect(line.token).toBeUndefined();
    expect(line.key).toBe(key);
    expect(line.problem).toBe(false);
  });

  it("marks only a running sync as busy", () => {
    expect(CLOUD_LINES.syncing.busy).toBe(true);
    expect(CLOUD_LINES.checking.busy).toBe(false);
  });
});
