/**
 * withToken: the one-run token is appended once, and the user's own
 * options are kept as they were.
 * restoreStep: the user's options come back only after Steam has read the
 * token (process created), never while Steam is still launching.
 */
import { describe, expect, it, vi } from "vitest";

// The launch half needs Steam and Decky; only the pure merge is tested.
vi.mock("./wrapper-shortcut-launch", () => ({ waitForLaunchOptions: vi.fn() }));

import { restoreStep, withToken } from "./one-run-launch";

const TOKEN = "UNIFIDECK_PLAY_TARGET=cloud";

describe("withToken", () => {
  it("adds the token to empty options", () => {
    expect(withToken("", TOKEN)).toBe(TOKEN);
  });

  it("keeps the user's options and appends the token", () => {
    expect(withToken("microsoft:ABC MANGOHUD=1 %command%", TOKEN)).toBe(
      `microsoft:ABC MANGOHUD=1 %command% ${TOKEN}`,
    );
  });

  it("replaces an earlier copy instead of stacking it", () => {
    expect(withToken("microsoft:ABC UNIFIDECK_PLAY_TARGET=local DXVK_HUD=1", TOKEN)).toBe(
      `microsoft:ABC DXVK_HUD=1 ${TOKEN}`,
    );
  });
});

describe("restoreStep", () => {
  const LAUNCHING = 1;
  const RUNNING = 4;
  const PLAY = 11;

  it("does not restore while Steam is still launching", () => {
    expect(restoreStep(LAUNCHING, false, false)).toBe("launching");
    expect(restoreStep(LAUNCHING, true, false)).toBe("launching");
  });

  it("restores once the game process is running", () => {
    expect(restoreStep(RUNNING, true, false)).toBe("restore");
  });

  it("waits before the launch has begun", () => {
    expect(restoreStep(PLAY, false, false)).toBe("wait");
    expect(restoreStep(undefined, false, false)).toBe("wait");
  });

  it("restores when a launch ends without running", () => {
    expect(restoreStep(PLAY, true, false)).toBe("restore");
  });

  it("restores on timeout whatever the status", () => {
    expect(restoreStep(LAUNCHING, true, true)).toBe("restore");
    expect(restoreStep(undefined, false, true)).toBe("restore");
  });
});
