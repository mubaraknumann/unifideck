// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

let routerMock: { WindowStore?: unknown } = {};
vi.mock("@decky/ui", () => ({
  get Router() {
    return routerMock;
  },
}));

import { navigateToApp } from "./navigate-to-app";

describe("navigateToApp", () => {
  const originalSteamUIStore = (window as unknown as { SteamUIStore?: unknown }).SteamUIStore;

  beforeEach(() => {
    routerMock = {};
  });

  afterEach(() => {
    (window as unknown as { SteamUIStore?: unknown }).SteamUIStore = originalSteamUIStore;
  });

  it("calls Navigator.App on the focused window instance", () => {
    const App = vi.fn();
    (window as unknown as { SteamUIStore?: unknown }).SteamUIStore = {
      GetFocusedWindowInstance: () => ({ Navigator: { App } }),
    };

    navigateToApp(12345);

    expect(App).toHaveBeenCalledWith(12345);
  });

  it("falls back to the Gamepad UI main window when nothing is focused", () => {
    const App = vi.fn();
    (window as unknown as { SteamUIStore?: unknown }).SteamUIStore = {
      GetFocusedWindowInstance: () => undefined,
    };
    routerMock = {
      WindowStore: { GamepadUIMainWindowInstance: { Navigator: { App } } },
    };

    navigateToApp(999);

    expect(App).toHaveBeenCalledWith(999);
  });

  it("falls back when GetFocusedWindowInstance throws", () => {
    const App = vi.fn();
    (window as unknown as { SteamUIStore?: unknown }).SteamUIStore = {
      GetFocusedWindowInstance: () => {
        throw new Error("boom");
      },
    };
    routerMock = {
      WindowStore: { GamepadUIMainWindowInstance: { Navigator: { App } } },
    };

    navigateToApp(1);

    expect(App).toHaveBeenCalledWith(1);
  });

  it("converts a signed sibling appid to unsigned before calling Navigator.App", () => {
    // appStore.m_mapApps (what Navigator.App resolves against) is keyed by
    // the UNSIGNED form on a live device — confirmed by dumping its keys.
    // GroupSibling.appId, read live off an actually-rendered switcher via
    // a React-fiber walk, turned out to be SIGNED (-584483551 for a real
    // shortcut whose unsigned form is 3710483745) — this is what silently
    // broke every click.
    const App = vi.fn();
    (window as unknown as { SteamUIStore?: unknown }).SteamUIStore = {
      GetFocusedWindowInstance: () => ({ Navigator: { App } }),
    };

    navigateToApp(-584483551);

    expect(App).toHaveBeenCalledWith(3710483745);
  });

  it("passes a small real Steam appid through unchanged (sign-invariant range)", () => {
    const App = vi.fn();
    (window as unknown as { SteamUIStore?: unknown }).SteamUIStore = {
      GetFocusedWindowInstance: () => ({ Navigator: { App } }),
    };

    navigateToApp(632470); // Disco Elysium's real Steam appid

    expect(App).toHaveBeenCalledWith(632470);
  });

  it("does not throw when no window with a Navigator can be found", () => {
    (window as unknown as { SteamUIStore?: unknown }).SteamUIStore = {
      GetFocusedWindowInstance: () => undefined,
    };
    routerMock = {};

    expect(() => navigateToApp(1)).not.toThrow();
  });
});
