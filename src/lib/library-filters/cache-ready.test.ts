// @vitest-environment jsdom
/**
 * `whenUnifideckCacheLoaded` + `UNIFIDECK_CACHE_UPDATED_EVENT` gate the
 * cloud-synced `[Unifideck]` collections. They must fire on a successful
 * cache load only: with an empty cache every store filter matches nothing,
 * and a collection sync run from that state emptied (and used to delete)
 * every store collection on boot, which broke TabMaster tabs built on them.
 *
 * The ready promise is module-level and resolves once, so each test imports
 * a fresh copy of the module.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

vi.mock("@decky/api", () => ({ call: vi.fn() }));
vi.mock("../../api/useRPC", () => ({
  unwrapRpcEnvelope: (raw: unknown) => raw,
}));
vi.mock("../protondb-cache", () => ({
  meetsGreatOnCurrentDevice: vi.fn(),
  getCachedCompatByTitle: vi.fn(),
  getCachedRating: vi.fn(),
  loadCompatCacheFromBackend: vi.fn(),
}));
vi.mock("../library-facets", () => ({
  getCompatByShortcutAppId: vi.fn(),
  loadFacets: vi.fn(),
}));
vi.mock("../device-type", () => ({
  activeCompatTrack: () => "deck",
}));
vi.mock("../../api/event-bus-client", () => ({
  EventBusClient: { subscribe: vi.fn() },
}));
vi.mock("../group-duplicates-setting", () => ({
  GROUP_DUPLICATES_EVENT: "unifideck:group-duplicates-change",
  isGroupDuplicatesEnabled: () => false,
}));

import { call } from "@decky/api";

const mockCall = vi.mocked(call);

async function freshModule() {
  vi.resetModules();
  return import("./index");
}

/** True if `p` has settled after the microtask queue drains. */
async function isSettled(p: Promise<unknown>): Promise<boolean> {
  let settled = false;
  void p.then(() => {
    settled = true;
  });
  await Promise.resolve();
  await Promise.resolve();
  return settled;
}

beforeEach(() => {
  mockCall.mockReset();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
});

describe("whenUnifideckCacheLoaded", () => {
  it("stays pending until the first successful load", async () => {
    const mod = await freshModule();
    const ready = mod.whenUnifideckCacheLoaded();
    expect(await isSettled(ready)).toBe(false);

    mockCall.mockResolvedValueOnce([]);
    await mod.loadUnifideckCache();

    expect(await isSettled(ready)).toBe(true);
  });

  it("does not resolve when every attempt fails", async () => {
    const mod = await freshModule();
    mockCall.mockRejectedValue(new Error("backend down"));

    await mod.loadUnifideckCache();
    // Burn the whole retry budget. The exhausted path latches
    // `isUnifideckCacheLoaded()` with an empty cache; the ready promise
    // must not follow it.
    await vi.advanceTimersByTimeAsync(60_000);

    expect(mod.isUnifideckCacheLoaded()).toBe(true);
    expect(await isSettled(mod.whenUnifideckCacheLoaded())).toBe(false);
  });
});

describe("UNIFIDECK_CACHE_UPDATED_EVENT", () => {
  it("fires after each successful load and not after a failed one", async () => {
    const mod = await freshModule();
    const onUpdated = vi.fn();
    window.addEventListener(mod.UNIFIDECK_CACHE_UPDATED_EVENT, onUpdated);
    try {
      mockCall.mockResolvedValueOnce([]);
      await mod.loadUnifideckCache();
      mockCall.mockResolvedValueOnce([]);
      await mod.loadUnifideckCache();
      expect(onUpdated).toHaveBeenCalledTimes(2);

      mockCall.mockRejectedValueOnce(new Error("transient"));
      await mod.loadUnifideckCache();
      expect(onUpdated).toHaveBeenCalledTimes(2);
    } finally {
      window.removeEventListener(mod.UNIFIDECK_CACHE_UPDATED_EVENT, onUpdated);
    }
  });
});
