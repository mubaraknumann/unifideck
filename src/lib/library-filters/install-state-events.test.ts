// @vitest-environment jsdom
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
  EventBusClient: {
    subscribe: vi.fn(),
  },
}));

import { unifideckGameCache, validThirdPartyCache, updateSingleGameStatus } from "./index";

// Overview enrichment listens for this event and re-sweeps, which is
// how an install/uninstall reaches Steam's live AppOverview.
describe("updateSingleGameStatus install-state propagation", () => {
  const onState = vi.fn();

  beforeEach(() => {
    unifideckGameCache.clear();
    validThirdPartyCache.clear();
    onState.mockClear();
    window.addEventListener("unifideck-game-state-changed", onState);
  });

  afterEach(() => {
    window.removeEventListener("unifideck-game-state-changed", onState);
  });

  function lastDetail(): unknown {
    return (onState.mock.lastCall?.[0] as CustomEvent).detail;
  }

  it("announces a newly added game", () => {
    updateSingleGameStatus({
      appId: 999,
      store: "epic",
      isInstalled: true,
    });

    expect(onState).toHaveBeenCalledTimes(1);
    expect(lastDetail()).toMatchObject({ appId: 999, isInstalled: true });
  });

  it("announces an install-state change", () => {
    unifideckGameCache.set(999, { store: "epic", isInstalled: true });

    updateSingleGameStatus({
      appId: 999,
      store: "epic",
      isInstalled: false,
    });

    expect(onState).toHaveBeenCalledTimes(1);
    expect(lastDetail()).toMatchObject({ appId: 999, isInstalled: false });
  });

  it("stays quiet when the state is unchanged", () => {
    unifideckGameCache.set(999, { store: "epic", isInstalled: true });

    updateSingleGameStatus({
      appId: 999,
      store: "epic",
      isInstalled: true,
    });

    expect(onState).not.toHaveBeenCalled();
  });
});
