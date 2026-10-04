// @vitest-environment node
/**
 * `get_protondb_cache` answers in the `{success, error, data}` envelope.
 * The loader iterated the envelope itself, so every build since 2026-05
 * logged `Loaded 0 title + 0 appId compat entries` while the backend
 * held ~1000 rows. These pin the wire shape.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { envelope } from "../test-support/rpc-envelope";

const ROWS = {
  "2270": {
    title: "",
    protondb_tier: "gold",
    compat_status: "playable",
    sources: ["protondb", "deck_verified"],
  },
  "1422440106": {
    title: "Ys I & II Chronicles+",
    protondb_tier: "platinum",
    compat_status: "verified",
    sources: ["protondb"],
  },
  "999": { title: "", protondb_tier: null, compat_status: "unknown", sources: [] },
};

const callMock = vi.fn<(...a: unknown[]) => Promise<unknown>>(async () => envelope(ROWS));
vi.mock("@decky/api", () => ({ call: (...a: unknown[]) => callMock(...a) }));

afterEach(() => {
  callMock.mockReset();
  callMock.mockImplementation(async () => envelope(ROWS));
  vi.resetModules();
});

describe("loadCompatCacheFromBackend", () => {
  it("reads the envelope and fills the appId-keyed ProtonDB map", async () => {
    const spy = vi.spyOn(console, "log").mockImplementation(() => {});
    const mod = await import("./protondb-cache");

    await mod.loadCompatCacheFromBackend();

    expect(mod.isCompatCacheLoaded()).toBe(true);
    expect(mod.getCachedRating(2270)).toBe("gold");
    expect(mod.getCachedRating(1422440106)).toBe("platinum");
    // No tier → no ProtonDB entry.
    expect(mod.getCachedRating(999)).toBeNull();
    expect(spy).toHaveBeenCalledWith(
      expect.stringContaining("Loaded 1 title + 2 appId compat entries"),
    );
    spy.mockRestore();
  });

  it("fills the title-keyed compat map only for rows that carry a title", async () => {
    const mod = await import("./protondb-cache");

    await mod.loadCompatCacheFromBackend();

    expect(mod.getCachedCompatByTitle("ys i & ii chronicles+")).toEqual({
      tier: "platinum",
      status: "verified",
      steamAppId: 1422440106,
    });
    expect(mod.getCompatCacheSize()).toBe(1);
  });

  it("treats a failed envelope as an empty cache, not a crash", async () => {
    callMock.mockImplementation(async () => envelope(null, false, "cache_cold"));
    const mod = await import("./protondb-cache");

    await mod.loadCompatCacheFromBackend();

    expect(mod.isCompatCacheLoaded()).toBe(true);
    expect(mod.getCachedRating(2270)).toBeNull();
  });

  it("tolerates a legacy pre-envelope reply", async () => {
    callMock.mockImplementation(async () => ROWS);
    const mod = await import("./protondb-cache");

    await mod.loadCompatCacheFromBackend();

    expect(mod.getCachedRating(2270)).toBe("gold");
  });
});
