import { describe, expect, it, vi } from "vitest";

vi.mock("@decky/ui", () => ({ showModal: () => {} }));
vi.mock("./useCloudSaveStatus", () => ({}));
vi.mock("./useToast", () => ({}));
vi.mock("../api/event-bus-client", () => ({}));
vi.mock("../api/cloud-save-pending", () => ({}));
vi.mock("../services/toast-text", () => ({}));
vi.mock("../components/modals/CloudSaveModal", () => ({}));

import { cloudSaveViewLine } from "./useCloudSaveView";

const view = (
  state: Parameters<typeof cloudSaveViewLine>[0]["state"],
  verdict: Parameters<typeof cloudSaveViewLine>[0]["verdict"] = null,
  cloudAvailable = false,
) => ({ state, verdict, cloudAvailable });

describe("cloudSaveViewLine", () => {
  it("shows nothing for a game without cloud saves", () => {
    expect(cloudSaveViewLine(view("no-support"))).toBeNull();
  });

  it("names which side is ahead from the verdict", () => {
    expect(cloudSaveViewLine(view("out-of-sync", "local_newer"))?.key).toBe(
      "play.cloudSave.statusWaitingUpload",
    );
    expect(cloudSaveViewLine(view("out-of-sync", "cloud_newer"))?.key).toBe(
      "play.cloudSave.statusCloudAvailable",
    );
  });

  it("falls back to which side has saves before the first sync", () => {
    expect(cloudSaveViewLine(view("out-of-sync", null, true))?.key).toBe(
      "play.cloudSave.statusCloudAvailable",
    );
    expect(cloudSaveViewLine(view("out-of-sync", null, false))?.key).toBe(
      "play.cloudSave.statusWaitingUpload",
    );
  });

  it("reads in sync for in-sync and empty", () => {
    for (const state of ["in-sync", "empty"] as const) {
      expect(cloudSaveViewLine(view(state))?.token).toBe("#AppDetails_CloudStatus_Synchronized");
    }
  });

  it("marks only a running sync as busy", () => {
    expect(cloudSaveViewLine(view("syncing"))?.busy).toBe(true);
    expect(cloudSaveViewLine(view("unresolved"))?.problem).toBe(true);
  });
});
