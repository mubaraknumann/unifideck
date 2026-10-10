/**
 * The status text a store row shows — the one place its rules live.
 *
 * `t` here echoes the key plus its interpolation values, so assertions
 * pin both which string was chosen and what was fed into it.
 */
import { describe, it, expect } from "vitest";
import type { TFunction } from "i18next";
import {
  formatAllStoresStatus,
  formatStoreStatus,
  isQueued,
  type StoreStatusInput,
} from "./storeStatus";
import type { StoreSyncRow } from "../../types/syncProgress";

const t = ((key: string, values?: Record<string, unknown>) =>
  values ? `${key}${JSON.stringify(values)}` : key) as unknown as TFunction;

function input(over: Partial<StoreStatusInput> = {}): StoreStatusInput {
  return {
    connected: true,
    queued: false,
    recentlyFinished: false,
    ...over,
  };
}

function row(over: Partial<StoreSyncRow>): StoreSyncRow {
  return {
    state: "active",
    phase: "games",
    done: 0,
    total: 0,
    error: null,
    ...over,
  };
}

describe("formatStoreStatus", () => {
  it("leaves a signed-out store blank, whatever else applies", () => {
    const line = formatStoreStatus(
      t,
      input({ connected: false, row: row({ state: "error" }), queued: true }),
    );
    expect(line).toEqual({ text: "", tone: "normal" });
  });

  it("shows fetch counts while a store's library is fetched", () => {
    const line = formatStoreStatus(
      t,
      input({ row: row({ phase: "games", done: 41, total: 120 }) }),
    );
    expect(line.text).toBe("storeConnections.phaseGames 41/120");
    expect(line.detail).toBe("storeConnections.phaseGames: 41 / 120");
  });

  it("shows a rounded-down percentage for the post-sync phases", () => {
    const line = formatStoreStatus(
      t,
      input({ row: row({ phase: "artwork", done: 210, total: 340 }) }),
    );
    expect(line.text).toBe("storeConnections.phaseArtwork 61%");
    expect(line.detail).toBe("storeConnections.phaseArtwork: 210 / 340");
  });

  it("does not divide by zero for an empty phase", () => {
    const line = formatStoreStatus(t, input({ row: row({ phase: "compat", done: 0, total: 0 }) }));
    expect(line.text).toBe("storeConnections.phaseCompat 0%");
  });

  it("names queued and waiting rows", () => {
    expect(formatStoreStatus(t, input({ row: row({ state: "queued" }) })).text).toBe(
      "storeConnections.queued",
    );
    expect(formatStoreStatus(t, input({ row: row({ state: "waiting" }) })).text).toBe(
      "storeConnections.waiting",
    );
  });

  it("keeps a failure red with its message as detail", () => {
    const line = formatStoreStatus(t, input({ row: row({ state: "error", error: "timeout" }) }));
    expect(line).toEqual({
      text: "storeConnections.failed",
      tone: "error",
      detail: "timeout",
    });
  });

  it("shows Done only right after the run finishes", () => {
    const done = row({ state: "done" });
    expect(formatStoreStatus(t, input({ row: done, recentlyFinished: true })).text).toBe(
      "storeConnections.done",
    );
    expect(
      formatStoreStatus(t, input({ row: done, summary: { count: 3, synced_at: null } })).text,
    ).toBe('storeConnections.gameCount{"count":3}');
  });

  it("shows Cancelled only right after the run is cancelled", () => {
    const cancelled = row({ state: "cancelled" });
    expect(formatStoreStatus(t, input({ row: cancelled, recentlyFinished: true })).text).toBe(
      "storeConnections.cancelled",
    );
  });

  it("marks a store that a queued request names", () => {
    expect(formatStoreStatus(t, input({ queued: true })).text).toBe("storeConnections.queued");
  });

  it("shows the game count when idle", () => {
    const line = formatStoreStatus(t, input({ summary: { count: 340, synced_at: 1 } }));
    expect(line).toEqual({
      text: 'storeConnections.gameCount{"count":340}',
      tone: "normal",
    });
  });

  it("says not synced yet without a summary", () => {
    expect(formatStoreStatus(t, input()).text).toBe("storeConnections.notSynced");
  });
});

describe("isQueued", () => {
  it("matches a named store or an all-stores request", () => {
    expect(isQueued(null, "epic")).toBe(false);
    expect(isQueued({ kind: "force", stores: ["gog"] }, "epic")).toBe(false);
    expect(isQueued({ kind: "force", stores: ["epic"] }, "epic")).toBe(true);
    expect(isQueued({ kind: "sync", stores: null }, "epic")).toBe(true);
  });
});

describe("formatAllStoresStatus", () => {
  it("counts finished stores during a run", () => {
    const line = formatAllStoresStatus(
      t,
      {
        epic: row({ state: "done" }),
        gog: row({ state: "error" }),
        amazon: row({ state: "active" }),
      },
      undefined,
      null,
      true,
    );
    expect(line.text).toBe('storeConnections.storesDone{"done":2,"total":3}');
  });

  it("says queued when every store is queued", () => {
    const line = formatAllStoresStatus(
      t,
      undefined,
      undefined,
      { kind: "sync", stores: null },
      false,
    );
    expect(line.text).toBe("storeConnections.queued");
  });

  it("totals the library when idle", () => {
    const line = formatAllStoresStatus(
      t,
      { epic: row({ state: "done" }) },
      {
        epic: { count: 10, synced_at: 1 },
        gog: { count: 5, synced_at: null },
      },
      null,
      false,
    );
    expect(line.text).toBe('storeConnections.gameCount{"count":15}');
  });
});
