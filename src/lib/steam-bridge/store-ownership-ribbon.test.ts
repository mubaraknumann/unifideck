// @vitest-environment jsdom
/**
 * Store-ownership ribbon detection: when the module calls the backend, and
 * when it must not. Steam's store browser is faked with the shape verified
 * on-device: `FinishedRequestCallbacks` and `StartLoadingCallbacks` are
 * getters returning callback lists whose `Register` hands back
 * `{ Unregister }`, and the browser object is created lazily.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { envelope } from "../../test-support/rpc-envelope";

const callMock = vi.fn();
vi.mock("@decky/api", () => ({ call: (...a: unknown[]) => callMock(...a) }));

const routerHolder: { window: unknown } = { window: undefined };
vi.mock("@decky/ui", () => ({
  Router: {
    get WindowStore() {
      return { GamepadUIMainWindowInstance: routerHolder.window };
    },
  },
}));
vi.mock("i18next", () => ({
  default: { t: (key: string) => `t:${key}`, dir: () => "ltr" },
}));

import {
  buildRibbonStrings,
  parseStoreAppId,
  startStoreOwnershipRibbon,
} from "./store-ownership-ribbon";
import { STORE_OWNERSHIP_ENABLED_KEY, setStoreOwnershipEnabled } from "../store-ownership-setting";

type Cb = (url: string, extra: unknown) => void;

function callbackList() {
  const callbacks: Cb[] = [];
  const unregister = vi.fn();
  const list = {
    Register: (cb: Cb) => {
      callbacks.push(cb);
      return {
        Unregister: () => {
          unregister();
          callbacks.splice(callbacks.indexOf(cb), 1);
        },
      };
    },
  };
  return { list, callbacks, unregister };
}

/** `startLoading: false` is a Steam build without `StartLoadingCallbacks`. */
function fakeBrowser(url = "https://store.steampowered.com/", { startLoading = true } = {}) {
  const finished = callbackList();
  const starting = callbackList();
  const browser: { m_URL: string } = {
    m_URL: url,
    get FinishedRequestCallbacks() {
      return finished.list;
    },
  } as { m_URL: string };
  if (startLoading) {
    Object.defineProperty(browser, "StartLoadingCallbacks", { get: () => starting.list });
  }
  const fire = (next: string) => {
    browser.m_URL = next;
    finished.callbacks.slice().forEach((cb) => cb(next, "title"));
  };
  const start = (next: string) => {
    browser.m_URL = next;
    starting.callbacks.slice().forEach((cb) => cb(next, false));
  };
  return {
    browser,
    callbacks: finished.callbacks,
    starting: starting.callbacks,
    unregister: finished.unregister,
    fire,
    start,
  };
}

function fakeWindow(storeBrowser?: unknown, pathname = "/library/home") {
  const listeners: Array<(u: unknown) => void> = [];
  const unlisten = vi.fn();
  const win = {
    m_StoreBrowser: storeBrowser,
    m_history: {
      location: { pathname },
      listen: (fn: (u: unknown) => void) => {
        listeners.push(fn);
        return unlisten;
      },
    },
  };
  const navigate = (path: string) => listeners.forEach((fn) => fn({ pathname: path }));
  return { win, navigate, unlisten, listeners };
}

const APP_URL = "https://store.steampowered.com/app/257350/Baldurs_Gate_II_Enhanced_Edition/";

beforeEach(() => {
  vi.useFakeTimers();
  callMock.mockReset();
  callMock.mockResolvedValue(envelope({ shown: true, reason: "shown", stores: ["gog"] }));
  window.localStorage.clear();
  routerHolder.window = undefined;
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("parseStoreAppId", () => {
  it.each([
    [APP_URL, 257350],
    ["https://store.steampowered.com/app/257350", 257350],
    ["https://store.steampowered.com/app/257350?snr=1_5_9__405", 257350],
    ["https://store.steampowered.com/app/257350#reviews", 257350],
    ["https://store.steampowered.com/agecheck/app/257350/", null],
    ["https://store.steampowered.com/", null],
    ["http://store.steampowered.com/app/257350/", null],
    ["https://store.steampowered.com.evil.example/app/257350/", null],
    [undefined, null],
    [42, null],
  ])("%s → %s", (url, expected) => {
    expect(parseStoreAppId(url)).toBe(expected);
  });
});

describe("buildRibbonStrings", () => {
  it("translates every key and carries the store labels", () => {
    const s = buildRibbonStrings();
    expect(s.tag_owned).toBe("t:storeOwnership.tagOwned");
    expect(s.message_cloud).toBe("t:storeOwnership.messageGamePass");
    expect(s.message_xcloud).toBe("t:storeOwnership.messageXboxCloud");
    expect(s.tag_cloud).toBe("t:storeOwnership.tagStreamable");
    expect(s.dir).toBe("ltr");
    expect(s.store_labels.gog).toBe("GOG");
    expect(s.store_labels.microsoft).toBe("Xbox");
    expect(s.note_labels.play_anywhere).toBe("t:storeOwnership.platformPlayAnywhere");
    expect(Object.keys(s.note_labels).sort()).toEqual([
      "cloud",
      "console",
      "gold",
      "pc",
      "pc_console",
      "play_anywhere",
    ]);
  });
});

describe("startStoreOwnershipRibbon", () => {
  it("handles the store page already open when it registers", async () => {
    const { browser } = fakeBrowser(APP_URL);
    routerHolder.window = fakeWindow(browser, "/steamweb").win;

    const dispose = startStoreOwnershipRibbon();
    await vi.advanceTimersByTimeAsync(300);

    expect(callMock).toHaveBeenCalledTimes(1);
    expect(callMock.mock.calls[0][0]).toBe("show_store_ownership");
    expect(callMock.mock.calls[0][1]).toBe(257350);
    expect(callMock.mock.calls[0][2].tag_owned).toBe("t:storeOwnership.tagOwned");
    dispose();
  });

  it("calls the backend on every app navigation, including a repeat (Back)", async () => {
    const fake = fakeBrowser();
    routerHolder.window = fakeWindow(fake.browser).win;
    const dispose = startStoreOwnershipRibbon();

    fake.fire(APP_URL);
    await vi.advanceTimersByTimeAsync(300);
    fake.fire("https://store.steampowered.com/app/1091500/");
    await vi.advanceTimersByTimeAsync(300);
    fake.fire(APP_URL);
    await vi.advanceTimersByTimeAsync(300);

    expect(callMock.mock.calls.map((c) => c[1])).toEqual([257350, 1091500, 257350]);
    dispose();
  });

  it("never calls the backend for non-app pages, and debounces bursts", async () => {
    const fake = fakeBrowser();
    routerHolder.window = fakeWindow(fake.browser).win;
    const dispose = startStoreOwnershipRibbon();

    fake.fire("https://store.steampowered.com/cart/");
    fake.fire(APP_URL);
    fake.fire("https://store.steampowered.com/agecheck/app/257350/");
    await vi.advanceTimersByTimeAsync(1000);

    expect(callMock).not.toHaveBeenCalled();
    dispose();
  });

  it("registers once the lazily created store browser appears", async () => {
    const fw = fakeWindow(undefined);
    routerHolder.window = fw.win;
    const dispose = startStoreOwnershipRibbon();
    const fake = fakeBrowser(APP_URL);

    fw.win.m_StoreBrowser = fake.browser;
    fw.navigate("/steamweb");
    await vi.advanceTimersByTimeAsync(300);

    expect(fake.callbacks).toHaveLength(1);
    expect(callMock).toHaveBeenCalledTimes(1);
    dispose();
  });

  it("moves its registration when Steam replaces the store browser", async () => {
    const first = fakeBrowser();
    const fw = fakeWindow(first.browser);
    routerHolder.window = fw.win;
    const dispose = startStoreOwnershipRibbon();
    const second = fakeBrowser();

    fw.win.m_StoreBrowser = second.browser;
    fw.navigate("/steamweb");

    expect(first.unregister).toHaveBeenCalledTimes(1);
    expect(second.callbacks).toHaveLength(1);
    dispose();
  });

  it("the disposer unregisters and unlistens", async () => {
    const fake = fakeBrowser();
    const fw = fakeWindow(fake.browser);
    routerHolder.window = fw.win;

    startStoreOwnershipRibbon()();
    fake.fire(APP_URL);
    await vi.advanceTimersByTimeAsync(1000);

    expect(fake.unregister).toHaveBeenCalledTimes(1);
    expect(fw.unlisten).toHaveBeenCalledTimes(1);
    expect(callMock).not.toHaveBeenCalled();
  });

  it("warns once and does not throw outside Big Picture", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    routerHolder.window = undefined;

    const dispose = startStoreOwnershipRibbon();
    await vi.advanceTimersByTimeAsync(60_000); // the window never appears
    window.dispatchEvent(
      new CustomEvent("unifideck:store-ownership-enabled-change", { detail: false }),
    );
    window.dispatchEvent(
      new CustomEvent("unifideck:store-ownership-enabled-change", { detail: true }),
    );
    await vi.advanceTimersByTimeAsync(60_000);

    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).toContain("GamepadUIMainWindowInstance");
    dispose();
  });

  it("waits for the Gaming Mode window after a Steam UI restart", async () => {
    // Regression: Decky reloaded the plugin before Steam rebuilt its window;
    // the ribbon gave up and stayed off for every store until a reload.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    routerHolder.window = undefined;
    const dispose = startStoreOwnershipRibbon();
    await vi.advanceTimersByTimeAsync(3_000);

    const { browser } = fakeBrowser(APP_URL);
    routerHolder.window = fakeWindow(browser, "/steamweb").win;
    // The next once-a-second check, plus the draw debounce.
    await vi.advanceTimersByTimeAsync(1_500);

    expect(callMock).toHaveBeenCalledTimes(1);
    expect(callMock.mock.calls[0][1]).toBe(257350);
    expect(warn).not.toHaveBeenCalled();
    dispose();
  });

  it("re-attaches when Steam rebuilds its Gaming Mode window", async () => {
    // Regression (2026-10-02): after the post-sync Steam UI restart the
    // ribbon held the old window and store browser, and was off for every
    // store page until a plugin reload.
    const first = fakeBrowser();
    const oldWindow = fakeWindow(first.browser);
    routerHolder.window = oldWindow.win;
    const dispose = startStoreOwnershipRibbon();

    const second = fakeBrowser();
    const newWindow = fakeWindow(second.browser);
    routerHolder.window = newWindow.win;
    await vi.advanceTimersByTimeAsync(1_000);

    expect(first.unregister).toHaveBeenCalledTimes(1);
    expect(oldWindow.unlisten).toHaveBeenCalledTimes(1);
    expect(newWindow.listeners).toHaveLength(1);
    expect(second.callbacks).toHaveLength(1);

    second.fire(APP_URL);
    await vi.advanceTimersByTimeAsync(300);
    expect(callMock).toHaveBeenCalledTimes(1);
    dispose();
  });

  it("draws as soon as a page starts loading", async () => {
    // Measured on a never-visited page: loading starts at ~0.9 s, the
    // page's blocks exist at ~2 s, and the finished signal comes at ~7 s.
    const fake = fakeBrowser();
    routerHolder.window = fakeWindow(fake.browser).win;
    const dispose = startStoreOwnershipRibbon();

    fake.start(APP_URL);
    await vi.advanceTimersByTimeAsync(300);

    expect(callMock).toHaveBeenCalledTimes(1);
    expect(callMock.mock.calls[0][1]).toBe(257350);
    dispose();
    expect(fake.starting).toHaveLength(0);
  });

  it("still works on a Steam build without the start-loading signal", async () => {
    const fake = fakeBrowser(undefined, { startLoading: false });
    routerHolder.window = fakeWindow(fake.browser).win;
    const dispose = startStoreOwnershipRibbon();

    fake.fire(APP_URL);
    await vi.advanceTimersByTimeAsync(300);

    expect(callMock).toHaveBeenCalledTimes(1);
    dispose();
  });

  it("does nothing while disabled, and attaches when enabled live", async () => {
    window.localStorage.setItem(STORE_OWNERSHIP_ENABLED_KEY, "0");
    const fake = fakeBrowser(APP_URL);
    routerHolder.window = fakeWindow(fake.browser).win;

    const dispose = startStoreOwnershipRibbon();
    expect(fake.callbacks).toHaveLength(0);

    setStoreOwnershipEnabled(true);
    await vi.advanceTimersByTimeAsync(300);

    expect(fake.callbacks).toHaveLength(1);
    expect(callMock).toHaveBeenCalledTimes(1);
    dispose();
  });

  it("warns once when the backend cannot reach CDP", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    callMock.mockResolvedValue(
      envelope({ shown: false, reason: "cdp_unavailable", stores: ["gog"] }),
    );
    const fake = fakeBrowser();
    routerHolder.window = fakeWindow(fake.browser).win;
    const dispose = startStoreOwnershipRibbon();

    fake.fire(APP_URL);
    await vi.advanceTimersByTimeAsync(300);
    fake.fire(APP_URL);
    await vi.advanceTimersByTimeAsync(300);

    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).toContain("cdp.port");
    dispose();
  });
});
