/**
 * Tests for the download store's event handling.
 *
 * Two behaviours, both consequences of `DOWNLOAD_*` becoming a
 * single-emitter family carrying one payload shape (audit item #4):
 *
 *  1. A progress event is applied to the row it names, matched on
 *     `item.id`. The old handler wrote every tick onto `queue.current`
 *     whatever game it belonged to — invisible only because the backend
 *     runs one download at a time.
 *  2. A failure toasts once, with the game's title. Epic and Amazon used
 *     to emit their own store-shaped `download_failed` in addition to the
 *     worker's, so a failed install on those two stores popped two
 *     toasts and the first had no title to show.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";

const mockCall = vi.fn();
const mockToast = vi.fn();
vi.mock("@decky/api", () => ({
  call: (...args: unknown[]) => mockCall(...args),
  toaster: { toast: (...args: unknown[]) => mockToast(...args) },
}));

// The store registers a handler per event; key them by name so a test can
// fire exactly the one it means.
const handlers = new Map<string, (payload: Record<string, unknown>) => void>();
vi.mock("../api/event-bus-client", () => ({
  EventBusClient: {
    subscribe: (name: string, handler: (payload: Record<string, unknown>) => void) => {
      handlers.set(name, handler);
      return vi.fn();
    },
    bumpToFast: vi.fn(),
  },
}));

vi.mock("i18next", () => ({
  default: { t: (key: string) => key },
}));
const mockInvalidateGameInfo = vi.fn();
const mockBumpGameStateVersion = vi.fn();
vi.mock("../hooks/useGameInfo", () => ({
  invalidateGameInfo: (id: number) => mockInvalidateGameInfo(id),
}));
vi.mock("../lib/game-state-version", () => ({
  bumpGameStateVersion: (id: number) => mockBumpGameStateVersion(id),
}));
vi.mock("../lib/game-size-cache", () => ({ invalidateGameSize: vi.fn() }));
vi.mock("../lib/download-errors", () => ({
  friendlyDownloadError: (raw: string) => `friendly:${raw}`,
}));
vi.mock("../utils/ubisoftShortcutLaunch", () => ({
  launchUbisoftInstallViaShortcut: vi.fn(),
}));
vi.mock("../utils/battlenetShortcutLaunch", () => ({
  launchBattlenetInstallViaShortcut: vi.fn(),
}));

import { downloadStore, mergeProgressIntoSnapshot, type DownloadSnapshot } from "./download-store";
import type { DownloadItem } from "../types/downloads";

/** A queue item as `DownloadItem.to_dict()` serialises it. */
function item(over: Partial<DownloadItem> = {}): DownloadItem {
  return {
    id: "epic:g1",
    game_id: "g1",
    game_title: "A Game",
    store: "epic",
    status: "running",
    progress_percent: 10,
    downloaded_bytes: 0,
    total_bytes: 0,
    speed_mbps: 0,
    eta_seconds: 0,
    added_time: 0,
    storage_location: "internal",
    ...over,
  } as DownloadItem;
}

function snapshot(current: DownloadItem | null): DownloadSnapshot {
  return {
    loading: false,
    queue: {
      success: true,
      queued: [],
      finished: [],
      current,
      state: current ? "running" : "idle",
    },
  };
}

describe("mergeProgressIntoSnapshot", () => {
  it("applies the tick to the row it names", () => {
    const prev = snapshot(item({ progress_percent: 10, speed_mbps: 0 }));

    const next = mergeProgressIntoSnapshot(prev, {
      item: item({ progress_percent: 42.5, speed_mbps: 2, eta_seconds: 90 }),
    });

    expect(next.queue?.current?.progress_percent).toBe(42.5);
    expect(next.queue?.current?.speed_mbps).toBe(2);
    expect(next.queue?.current?.eta_seconds).toBe(90);
  });

  it("carries the phase through, so the row doesn't wait for a refetch", () => {
    const prev = snapshot(item({ download_phase: "downloading" }));

    const next = mergeProgressIntoSnapshot(prev, {
      item: item({ download_phase: "verifying" }),
    });

    expect(next.queue?.current?.download_phase).toBe("verifying");
  });

  it("ignores a tick for a different game, without re-rendering", () => {
    const prev = snapshot(item({ id: "epic:g1", progress_percent: 10 }));

    const next = mergeProgressIntoSnapshot(prev, {
      item: item({ id: "gog:g2", game_id: "g2", progress_percent: 99 }),
    });

    // Same object: `useSyncExternalStore` skips the re-render entirely.
    expect(next).toBe(prev);
    expect(next.queue?.current?.progress_percent).toBe(10);
  });

  it("ignores a payload with no item and one with no row on screen", () => {
    const prev = snapshot(item());

    expect(mergeProgressIntoSnapshot(prev, {})).toBe(prev);
    expect(mergeProgressIntoSnapshot(prev, { item: { store: "epic" } })).toBe(prev);
    const empty = snapshot(null);
    expect(mergeProgressIntoSnapshot(empty, { item: item() })).toBe(empty);
  });
});

// The store toasts once per event it receives, so the count below pins the
// handler, not the de-duplication — that lives at the source and is covered
// by `tests/unit/test_download_event_single_emitter.py` plus the emitter
// allowlist in `scripts/validate_event_schemas.py`. What this pins on the
// frontend is that the surviving payload is the one that can name the game.
describe("download_failed handling", () => {
  beforeEach(() => {
    mockCall.mockReset();
    mockCall.mockResolvedValue({ success: true, error: null, data: {} });
    mockToast.mockReset();
    downloadStore.start();
  });

  it("toasts once, naming the game, from the item's error", () => {
    handlers.get("download_failed")?.({
      item: item({
        status: "failed",
        error_message: "legendary_exit_1: no asset found",
      }),
      error: "legendary_exit_1: no asset found",
      error_type: "unknown_error",
    });

    expect(mockToast).toHaveBeenCalledTimes(1);
    const toast = mockToast.mock.calls[0][0] as {
      title: string;
      body: string;
    };
    // The title the store-shaped duplicate could never produce.
    expect(toast.title).toContain("A Game");
    expect(toast.body).toBe("friendly:legendary_exit_1: no asset found");
  });
});

/**
 * The "Install" button on a game that just installed.
 *
 * Reported after cancelling Among Us's prefix warmup: the details page kept
 * offering Install until it was closed and reopened. DOWNLOAD_COMPLETE fires
 * before the backend's post-install hook flips the shortcut — 5.9 s before it,
 * on that install — so the refetch it triggers caches `is_installed: false`,
 * and no later download event exists to correct it.
 */
describe("install-state invalidation", () => {
  beforeEach(() => {
    mockCall.mockReset();
    mockCall.mockResolvedValue({ success: true, error: null, data: {} });
    mockInvalidateGameInfo.mockReset();
    mockBumpGameStateVersion.mockReset();
    downloadStore.start();
  });

  it("drops the cached game info when the shortcut actually flips", () => {
    handlers.get("shortcut_install_state_changed")?.({
      app_id: -910147527,
      store: "epic",
      store_game_id: "963137e4c29d4c79a81323b8fab03a40",
      installed: true,
      install_path: "/games/AmongUs",
      exe_path: "/games/AmongUs/Among Us.exe",
    });

    expect(mockInvalidateGameInfo).toHaveBeenCalledWith(-910147527);
    expect(mockBumpGameStateVersion).toHaveBeenCalledWith(-910147527);
  });

  it("ignores a payload with no usable app_id", () => {
    handlers.get("shortcut_install_state_changed")?.({ store: "epic" });

    expect(mockInvalidateGameInfo).not.toHaveBeenCalled();
    expect(mockBumpGameStateVersion).not.toHaveBeenCalled();
  });
});
