/**
 * withToken: the one-run token is appended once, and the user's own
 * options are kept as they were.
 */
import { describe, expect, it, vi } from "vitest";

// The launch half needs Steam and Decky; only the pure merge is tested.
vi.mock("./wrapper-shortcut-launch", () => ({ waitForLaunchOptions: vi.fn() }));

import { withToken } from "./one-run-launch";

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
