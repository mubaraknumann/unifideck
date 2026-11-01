/**
 * A failed `get_game_info` must never stick.
 *
 * Decky serves RPCs while the backend is still booting, and after a plugin
 * restart or update an open App-Details page asks right away. The hook used
 * to cache that failure as "no data" and never ask again, so the page sat on
 * the loading skeleton (with Steam's own Play row hidden) until the user
 * left and came back. These tests pin the two pieces that fix it: the cache
 * forgets failures, and the fetch is retried with backoff.
 *
 * The hook itself cannot render under the React stub, so the retry and
 * cache logic live in plain exported functions and are tested directly.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@decky/api", () => ({ call: vi.fn() }));

import {
  _clearGameInfoCache,
  fetchGameInfoCached,
  fetchWithRetry,
  type RetryOptions,
  xboxPcOf,
} from "./useGameInfo";
import type { Game } from "../types/api";

const APP_ID = 3_012_345_678;

const GAME = { id: "1", store_game_id: "1", title: "T", store: "gog" } as Game;

/** Retry options on a fake clock: `sleep` advances `now` instantly. */
function fakeClock(over: Partial<RetryOptions> = {}): RetryOptions & {
  slept: number[];
} {
  let t = 0;
  const slept: number[] = [];
  return {
    delaysMs: [500, 1000, 2000],
    budgetMs: 30_000,
    now: () => t,
    sleep: async (ms) => {
      slept.push(ms);
      t += ms;
    },
    isCancelled: () => false,
    slept,
    ...over,
  };
}

beforeEach(() => {
  _clearGameInfoCache();
});

describe("fetchGameInfoCached — failures are not cached", () => {
  it("asks the backend again after a failure instead of serving null", async () => {
    const fetch = vi
      .fn<(id: number) => Promise<Game | null>>()
      .mockRejectedValueOnce(new Error("backend still starting"))
      .mockResolvedValueOnce(GAME);

    await expect(fetchGameInfoCached(APP_ID, fetch, false)).rejects.toThrow();
    await expect(fetchGameInfoCached(APP_ID, fetch, false)).resolves.toBe(GAME);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("keeps earlier good data when a refresh fails", async () => {
    const fetch = vi
      .fn<(id: number) => Promise<Game | null>>()
      .mockResolvedValueOnce(GAME)
      .mockRejectedValueOnce(new Error("down"))
      .mockResolvedValueOnce(null);

    await fetchGameInfoCached(APP_ID, fetch, false);
    await expect(fetchGameInfoCached(APP_ID, fetch, true)).rejects.toThrow();
    // The failed refresh did not wipe the entry: a forced refetch still
    // goes to the backend, and its answer replaces the old data.
    await expect(fetchGameInfoCached(APP_ID, fetch, true)).resolves.toBeNull();
  });

  it("shares one in-flight request between concurrent callers", async () => {
    let resolve!: (g: Game) => void;
    const fetch = vi.fn(() => new Promise<Game | null>((r) => (resolve = r)));

    const a = fetchGameInfoCached(APP_ID, fetch, false);
    const b = fetchGameInfoCached(APP_ID, fetch, false);
    resolve(GAME);

    await expect(a).resolves.toBe(GAME);
    await expect(b).resolves.toBe(GAME);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

describe("fetchWithRetry", () => {
  it("recovers once the backend comes up", async () => {
    const clock = fakeClock();
    const attempt = vi
      .fn<() => Promise<Game | null>>()
      .mockRejectedValueOnce(new Error("starting"))
      .mockRejectedValueOnce(new Error("starting"))
      .mockResolvedValueOnce(GAME);

    await expect(fetchWithRetry(attempt, clock)).resolves.toBe(GAME);
    expect(attempt).toHaveBeenCalledTimes(3);
    expect(clock.slept).toEqual([500, 1000]);
  });

  it("treats a null answer as final: not a Unifideck game, no retry", async () => {
    const attempt = vi.fn(async () => null);

    await expect(fetchWithRetry(attempt, fakeClock())).resolves.toBeNull();
    expect(attempt).toHaveBeenCalledTimes(1);
  });

  it("repeats the last delay and gives up when the budget is spent", async () => {
    const clock = fakeClock({ budgetMs: 6000 });
    const attempt = vi.fn(async (): Promise<Game | null> => {
      throw new Error("down");
    });

    await expect(fetchWithRetry(attempt, clock)).rejects.toThrow("down");
    // 500 + 1000 + 2000 + 2000 = 5500; one more 2000 would pass 6000.
    expect(clock.slept).toEqual([500, 1000, 2000, 2000]);
  });

  it("stops retrying when the page goes away", async () => {
    let cancelled = false;
    const clock = fakeClock({
      isCancelled: () => cancelled,
      sleep: async () => {
        cancelled = true;
      },
    });
    const attempt = vi.fn(async (): Promise<Game | null> => {
      throw new Error("down");
    });

    await expect(fetchWithRetry(attempt, clock)).rejects.toThrow();
    expect(attempt).toHaveBeenCalledTimes(1);
  });
});

describe("xboxPcOf", () => {
  it("reads metadata.xbox_pc when it carries a state", () => {
    const info = { v: 1, state: "ready", product_id: "9PK087LNGJC5" };
    expect(xboxPcOf({ metadata: { xbox_pc: info } })).toBe(info);
  });

  it("ignores rows without it, or with a malformed one", () => {
    expect(xboxPcOf({})).toBeUndefined();
    expect(xboxPcOf({ metadata: { browser_url: "x" } })).toBeUndefined();
    expect(xboxPcOf({ metadata: { xbox_pc: { v: 1 } } })).toBeUndefined();
    expect(xboxPcOf({ metadata: { xbox_pc: "ready" } })).toBeUndefined();
  });
});
