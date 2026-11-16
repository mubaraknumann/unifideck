/**
 * sync-store — the progress block must not freeze on the last poll taken
 * before the final post-sync phase finished.
 *
 * Post-sync counters (artwork, metadata, compat) reach the frontend only
 * through the 500ms `get_sync_progress` poll. The backend marks the run
 * complete in the same step that emits the last `post_sync_phase_changed`,
 * so the store used to stop polling one tick behind and keep
 * "Checking compatibility data… 1304 / 1305" on screen for good.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

vi.mock("@decky/api", () => ({ call: vi.fn() }));
vi.mock("@decky/ui", () => ({ showModal: vi.fn() }));
vi.mock("../components/modals/SteamRestartModal", () => ({
  SteamRestartModal: () => null,
}));
vi.mock("../hooks/useSyncCooldown", () => ({ setSyncCooldownMs: vi.fn() }));

type Handler = (payload: Record<string, unknown>) => void;
const handlers = new Map<string, Handler>();
vi.mock("../api/event-bus-client", () => ({
  EventBusClient: {
    subscribe: vi.fn((event: string, fn: Handler) => {
      handlers.set(event, fn);
      return () => handlers.delete(event);
    }),
    bumpToFast: vi.fn(),
  },
}));

import { call } from "@decky/api";
import { syncStore } from "./sync-store";

const callMock = vi.mocked(call);

function status(fields: Record<string, unknown>): unknown {
  return { success: true, error: null, data: fields };
}

const MID_COMPAT = {
  status: "proton_meta",
  syncing: true,
  compat_total: 1305,
  compat_synced: 1304,
};
const COMPLETE = {
  status: "complete",
  syncing: false,
  compat_total: 1305,
  compat_synced: 1305,
};

/** Let the store's un-awaited `_pollOnce()` promises settle. */
async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

function emit(event: string, payload: Record<string, unknown> = {}): void {
  const fn = handlers.get(event);
  if (!fn) throw new Error(`no subscriber for ${event}`);
  fn(payload);
}

describe("sync-store final post-sync phase", () => {
  beforeEach(async () => {
    vi.useFakeTimers();
    callMock.mockReset();
    callMock.mockResolvedValue(status({ status: "idle", syncing: false }));
    syncStore.start();
    await flush();
    emit("sync_started", { registered_phases: ["proton_meta"], run_id: 7 });
    callMock.mockResolvedValue(status(MID_COMPAT));
    await vi.advanceTimersByTimeAsync(500);
    expect(syncStore.getSnapshot().progress?.compat_synced).toBe(1304);
  });

  afterEach(() => {
    syncStore.stop();
    handlers.clear();
    vi.useRealTimers();
  });

  it("fetches the finished state instead of freezing on the last poll", async () => {
    callMock.mockResolvedValue(status(COMPLETE));
    emit("post_sync_phase_changed", { phase: "proton_meta", active: false, run_id: 7 });
    await flush();

    const snap = syncStore.getSnapshot();
    expect(snap.isSyncing).toBe(false);
    expect(snap.progress?.status).toBe("complete");
    expect(snap.progress?.compat_synced).toBe(1305);
  });

  it("stops polling once the backend reports the run finished", async () => {
    callMock.mockResolvedValue(status(COMPLETE));
    emit("post_sync_phase_changed", { phase: "proton_meta", active: false, run_id: 7 });
    await flush();
    const calls = callMock.mock.calls.length;

    await vi.advanceTimersByTimeAsync(5000);
    expect(callMock.mock.calls.length).toBe(calls);
  });

  it("keeps polling until the backend catches up", async () => {
    // The phase-done event can beat the backend's mark_complete by a tick.
    callMock.mockResolvedValueOnce(status(MID_COMPAT));
    callMock.mockResolvedValue(status(COMPLETE));
    emit("post_sync_phase_changed", { phase: "proton_meta", active: false, run_id: 7 });
    await flush();
    expect(syncStore.getSnapshot().progress?.status).toBe("proton_meta");

    await vi.advanceTimersByTimeAsync(500);
    const snap = syncStore.getSnapshot();
    expect(snap.progress?.status).toBe("complete");
    expect(snap.isSyncing).toBe(false);
  });
});

describe("sync-store progress events", () => {
  beforeEach(async () => {
    vi.useFakeTimers();
    callMock.mockReset();
    callMock.mockResolvedValue(status({ status: "idle", syncing: false }));
    syncStore.start();
    await flush();
  });

  afterEach(() => {
    syncStore.stop();
    handlers.clear();
    vi.useRealTimers();
  });

  it("keeps the store rows and idle summary when a sync_progress event lands", async () => {
    callMock.mockResolvedValue(
      status({
        status: "syncing",
        syncing: true,
        stores: { gog: { state: "active", phase: "games", done: 0, total: 0, error: null } },
        store_summary: { epic: { count: 456, synced_at: null } },
        queued: null,
      }),
    );
    emit("sync_started", { registered_phases: [], run_id: 1 });
    await vi.advanceTimersByTimeAsync(500);
    expect(syncStore.getSnapshot().progress?.store_summary?.epic?.count).toBe(456);

    // The event's payload has no stores / store_summary / queued.
    emit("sync_progress", { store: "gog", progress_percent: 10, status: "syncing" });

    const progress = syncStore.getSnapshot().progress;
    expect(progress?.progress_percent).toBe(10);
    expect(progress?.store_summary?.epic?.count).toBe(456);
    expect(progress?.stores?.gog?.state).toBe("active");
  });
});
