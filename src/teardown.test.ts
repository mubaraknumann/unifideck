/**
 * Regression tests for plugin unload disposing every captured handle.
 *
 * `overviewEnrichment` was declared on `TeardownHandles` and assigned at
 * boot, but `runTeardown` never called it. Because `EventBusClient` only
 * stops polling once its subscriber set empties, that one omission left an
 * immortal 2s `subscribe_replay` loop behind on every plugin reload, and
 * `startUnifideckCacheAutoload` had no disposer at all.
 *
 * The structural guard is the total `Record<keyof TeardownHandles, …>` in
 * teardown.ts: a new handle without a disposer is now a compile error.
 * These tests are the runtime backstop for that, and they assert the
 * behaviour a type can't — that one throwing disposer cannot cost the
 * others.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";

const stops = {
  sync: vi.fn(),
  download: vi.fn(),
  auth: vi.fn(),
  storeInfo: vi.fn(),
};

vi.mock("./stores/sync-store", () => ({ syncStore: { stop: () => stops.sync() } }));
vi.mock("./stores/download-store", () => ({
  downloadStore: { stop: () => stops.download() },
}));
vi.mock("./stores/auth-store", () => ({ authStore: { stop: () => stops.auth() } }));
vi.mock("./stores/store-info-store", () => ({
  storeInfoStore: { stop: () => stops.storeInfo() },
}));

/** Every key of `TeardownHandles`, with the shape each one disposes by.
 *  Keep this in step with the interface — a key missing here means the
 *  test silently stops covering it. */
function buildHandles() {
  const calls: Record<string, ReturnType<typeof vi.fn>> = {};
  const fn = (name: string) => (calls[name] = vi.fn());
  return {
    calls,
    handles: {
      routerPatch: { remove: fn("routerPatch") },
      cacheAutoload: fn("cacheAutoload"),
      libraryPatch: { remove: fn("libraryPatch") },
      collectionManager: { remove: fn("collectionManager") },
      appStorePatch: { remove: fn("appStorePatch") },
      overviewEnrichment: fn("overviewEnrichment"),
      tileStoreBadgePatch: fn("tileStoreBadgePatch"),
      appContextMenuPatch: { unpatch: fn("appContextMenuPatch") },
      lifetimeListener: { unregister: fn("lifetimeListener") },
      launcherToastPoll: fn("launcherToastPoll"),
      pluginUpdateNotice: fn("pluginUpdateNotice"),
      bootEventListener: fn("bootEventListener"),
    },
  };
}

beforeEach(() => {
  Object.values(stops).forEach((s) => s.mockReset());
});

describe("runTeardown", () => {
  it("disposes every captured handle", async () => {
    const { runTeardown } = await import("./teardown");
    const { calls, handles } = buildHandles();

    runTeardown(handles as never);

    for (const [name, spy] of Object.entries(calls)) {
      expect(spy, `${name} was never disposed`).toHaveBeenCalledTimes(1);
    }
  });

  it("covers every key the DISPOSERS table declares", async () => {
    // Guards the test itself: if a handle is added to TeardownHandles (and
    // so to DISPOSERS, which tsc enforces) but not to buildHandles above,
    // this fails rather than quietly reducing coverage.
    const { runTeardown } = await import("./teardown");
    const { calls, handles } = buildHandles();
    runTeardown(handles as never);
    expect(Object.keys(calls).sort()).toEqual(Object.keys(handles).sort());
  });

  it("stops the boot-time stores, which hold their own poll timers", async () => {
    const { runTeardown } = await import("./teardown");
    runTeardown({});
    expect(stops.sync).toHaveBeenCalledTimes(1);
    expect(stops.download).toHaveBeenCalledTimes(1);
    expect(stops.auth).toHaveBeenCalledTimes(1);
    expect(stops.storeInfo).toHaveBeenCalledTimes(1);
  });

  it("tolerates absent handles", async () => {
    const { runTeardown } = await import("./teardown");
    expect(() => runTeardown({})).not.toThrow();
  });

  it("keeps going when one disposer throws", async () => {
    const { runTeardown } = await import("./teardown");
    const { calls, handles } = buildHandles();
    handles.appStorePatch.remove = vi.fn(() => {
      throw new Error("patch already gone");
    });

    expect(() => runTeardown(handles as never)).not.toThrow();
    // The one registered *after* the thrower must still be released, or a
    // single stale Steam patch would strand every earlier subscription.
    expect(calls.routerPatch).toHaveBeenCalledTimes(1);
    expect(calls.overviewEnrichment).toHaveBeenCalledTimes(1);
  });
});
