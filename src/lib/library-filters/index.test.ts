// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

vi.mock("@decky/api", () => ({ call: vi.fn() }));
vi.mock("../../api/useRPC", () => ({
  unwrapRpcEnvelope: (raw: unknown) => raw,
}));
vi.mock("../protondb-cache", () => ({
  meetsGreatOnDeckCriteria: vi.fn(),
  getCachedCompatByTitle: vi.fn(),
  getCachedRating: vi.fn(),
  loadCompatCacheFromBackend: vi.fn(),
}));
vi.mock("../library-facets", () => ({
  getCompatByShortcutAppId: vi.fn(),
  loadFacets: vi.fn(),
}));
vi.mock("../../api/event-bus-client", () => ({
  EventBusClient: {
    subscribe: vi.fn(),
  },
}));
vi.mock("../steam-bridge/overview-enrichment", () => ({
  enrichInstalledState: vi.fn(),
}));

import { call } from "@decky/api";
import {
  runFilter,
  unifideckGameCache,
  validThirdPartyCache,
  loadUnifideckCache,
  isUnifideckCacheLoaded,
  updateSingleGameStatus,
} from "./index";
import { enrichInstalledState } from "../steam-bridge/overview-enrichment";
import type { SteamAppOverview } from "../../types/steam";

const NON_STEAM_APP_TYPE = 1073741824;
const mockCall = vi.mocked(call);

describe("library-filters/index.ts installed filter", () => {
  beforeEach(() => {
    unifideckGameCache.clear();
    validThirdPartyCache.clear();
  });

  it("includes an installed Steam game", () => {
    const app = {
      appid: 12345,
      app_type: 1, // Native Steam Game
      installed: true,
      display_name: "Steam Game",
    } as unknown as SteamAppOverview;

    const result = runFilter({ type: "installed", params: { installed: true } }, app);
    expect(result).toBe(true);
  });

  it("excludes an uninstalled Steam game", () => {
    const app = {
      appid: 12345,
      app_type: 1,
      installed: false,
      display_name: "Steam Game",
    } as unknown as SteamAppOverview;

    const result = runFilter({ type: "installed", params: { installed: true } }, app);
    expect(result).toBe(false);
  });

  it("includes an installed Unified game", () => {
    unifideckGameCache.set(999, {
      store: "epic",
      isInstalled: true,
    });

    const app = {
      appid: 999,
      app_type: NON_STEAM_APP_TYPE,
      installed: true,
      display_name: "Unified Game",
    } as unknown as SteamAppOverview;

    const result = runFilter({ type: "installed", params: { installed: true } }, app);
    expect(result).toBe(true);
  });

  it("excludes an uninstalled Unified game", () => {
    unifideckGameCache.set(999, {
      store: "epic",
      isInstalled: false,
    });

    const app = {
      appid: 999,
      app_type: NON_STEAM_APP_TYPE,
      installed: true, // Steam might report it as installed because it's a shortcut
      display_name: "Unified Game",
    } as unknown as SteamAppOverview;

    const result = runFilter({ type: "installed", params: { installed: true } }, app);
    expect(result).toBe(false);
  });

  it("excludes non-Unifideck third-party shortcuts", () => {
    const app = {
      appid: 777,
      app_type: NON_STEAM_APP_TYPE,
      installed: true,
      display_name: "Custom Shortcut",
    } as unknown as SteamAppOverview;

    const result = runFilter({ type: "installed", params: { installed: true } }, app);
    expect(result).toBe(false);
  });
});

describe("loadUnifideckCache fail-open (UD-043 / UD-008)", () => {
  beforeEach(async () => {
    // Reset the module-level retry counter via a successful load so
    // each test starts from a clean retry budget regardless of order.
    mockCall.mockReset();
    mockCall.mockResolvedValueOnce([]);
    await loadUnifideckCache();
    unifideckGameCache.clear();
    mockCall.mockReset();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  const okRow = (appId: number) => ({
    app_id: appId,
    store: "epic",
    installed: true,
    store_game_id: String(appId),
  });

  it("populates the cache on a successful RPC", async () => {
    mockCall.mockResolvedValueOnce([okRow(999)]);
    await loadUnifideckCache();
    expect(unifideckGameCache.has(999)).toBe(true);
    expect(isUnifideckCacheLoaded()).toBe(true);
  });

  it("does NOT wipe an existing cache when the RPC fails", async () => {
    // Seed a good cache first.
    mockCall.mockResolvedValueOnce([okRow(999)]);
    await loadUnifideckCache();
    expect(unifideckGameCache.has(999)).toBe(true);

    // A later transient failure must not clear the previously-loaded
    // games — that was the UD-043 "synced but 0 shown" bug.
    mockCall.mockRejectedValueOnce(new Error("network down"));
    await loadUnifideckCache();
    expect(unifideckGameCache.has(999)).toBe(true);
  });

  it("retries with backoff after a failure, then succeeds", async () => {
    // First call rejects, the scheduled retry resolves.
    mockCall.mockRejectedValueOnce(new Error("boom")).mockResolvedValueOnce([okRow(1234)]);

    await loadUnifideckCache();
    expect(unifideckGameCache.has(1234)).toBe(false); // not yet

    // Advance past the backoff window (generous — the base delay
    // scales with the module-level retry count, which other tests in
    // this file may have bumped). The scheduled retry fires + resolves.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(unifideckGameCache.has(1234)).toBe(true);
  });
});

describe("updateSingleGameStatus install-state propagation", () => {
  beforeEach(() => {
    unifideckGameCache.clear();
    validThirdPartyCache.clear();
    vi.mocked(enrichInstalledState).mockClear();
  });

  it("calls enrichInstalledState when a new game is added", () => {
    updateSingleGameStatus({
      appId: 999,
      store: "epic",
      isInstalled: true,
    });

    expect(enrichInstalledState).toHaveBeenCalledWith(999, true);
  });

  it("calls enrichInstalledState when install state changes", () => {
    unifideckGameCache.set(999, { store: "epic", isInstalled: true });

    updateSingleGameStatus({
      appId: 999,
      store: "epic",
      isInstalled: false,
    });

    expect(enrichInstalledState).toHaveBeenCalledWith(999, false);
  });

  it("does NOT call enrichInstalledState when state is unchanged", () => {
    unifideckGameCache.set(999, { store: "epic", isInstalled: true });

    updateSingleGameStatus({
      appId: 999,
      store: "epic",
      isInstalled: true,
    });

    expect(enrichInstalledState).not.toHaveBeenCalled();
  });
});
