/**
 * runXboxPcSetup: an install must not be refused on a stale "not signed in",
 * and must not continue before the runtime is there.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const h = vi.hoisted(() => ({
  signedIn: false,
  refresh: vi.fn(async () => {}),
  link: vi.fn(async () => true),
  waitForRuntime: vi.fn(async () => true),
  toast: vi.fn(),
}));

vi.mock("../toast-text", () => ({ toastText: (k: string) => k }));
vi.mock("../../stores/xbox-pc-store", () => ({
  xboxPcStore: {
    getSnapshot: () => ({ status: { xcloud_signed_in: h.signedIn } }),
    refresh: h.refresh,
  },
}));
vi.mock("./xbox-pc-actions", () => ({ xboxPcToast: h.toast }));
vi.mock("./xbox-pc-link", () => ({
  linkXboxPc: h.link,
  waitForRuntime: h.waitForRuntime,
}));
vi.mock("../../lib/xbox-pc-setup", () => ({}));

import { runXboxPcSetup } from "./xbox-pc-flow";

beforeEach(() => {
  vi.clearAllMocks();
  h.signedIn = false;
});

describe("runXboxPcSetup", () => {
  it("re-checks a stale false before refusing, and carries on", async () => {
    h.refresh.mockImplementationOnce(async () => {
      h.signedIn = true;
    });
    expect(await runXboxPcSetup()).toBe(true);
    expect(h.toast).not.toHaveBeenCalled();
    expect(h.link).toHaveBeenCalledTimes(1);
  });

  it("toasts when the re-check still says signed out", async () => {
    expect(await runXboxPcSetup()).toBe(false);
    expect(h.toast).toHaveBeenCalledTimes(1);
    expect(h.link).not.toHaveBeenCalled();
  });

  it("waits for the runtime after the link", async () => {
    h.signedIn = true;
    h.waitForRuntime.mockResolvedValueOnce(false);
    expect(await runXboxPcSetup()).toBe(false);
    expect(h.refresh).not.toHaveBeenCalled();
  });
});
