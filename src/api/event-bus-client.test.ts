/**
 * Regression tests for the first-poll "prime past" behaviour.
 *
 * On a Steam restart the Decky backend (and its in-memory event replay
 * buffer) keeps running, so the next plugin load would replay a prior
 * session's whole sync lifecycle from timestamp 0 — resurrecting the
 * progress bar and re-showing the restart modal. The fix primes past the
 * STALE_ON_RELOAD_EVENTS (and IMPERATIVE_EVENTS) on the first poll while
 * still dispatching them when they arrive live during the session.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

// `call` is the only @decky/api binding event-bus-client pulls in (directly
// and via useRPC). Route it through a vi.fn we control per test.
const mockCall = vi.fn();
vi.mock("@decky/api", () => ({
  call: (...args: unknown[]) => mockCall(...args),
}));

// React is peer-provided by the Steam webview and unresolvable under vitest.
// The class under test never touches it at import time (only the useEventBus
// hook does, which these tests don't exercise) — stub the named imports it
// and useRPC pull in.
vi.mock("react", () => ({
  useEffect: () => {},
  useRef: <T>(v: T) => ({ current: v }),
  useCallback: <T>(fn: T) => fn,
  useState: <T>(v: T) => [v, () => {}],
}));

// The first poll is scheduled at POLL_SLOW_MS (2s); subsequent polls at
// POLL_FAST_MS (250ms) when events arrived.
const POLL_SLOW_MS = 2000;
const POLL_FAST_MS = 250;

interface Rec {
  event: string;
  kwargs: Record<string, unknown>;
  timestamp: number;
}

beforeEach(() => {
  // Fresh singleton per test — primed/lastSeenTimestamp must reset.
  vi.resetModules();
  mockCall.mockReset();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("EventBusClient first-poll priming", () => {
  it("does NOT dispatch stale sync events on the first poll after load", async () => {
    const { EventBusClient } = await import("./event-bus-client");
    const started = vi.fn();
    const progress = vi.fn();
    const reconcile = vi.fn();
    EventBusClient.subscribe("sync_started", started);
    EventBusClient.subscribe("sync_progress", progress);
    EventBusClient.subscribe("shortcut_reconcile_complete", reconcile);

    const buffered: Rec[] = [
      { event: "sync_started", kwargs: {}, timestamp: 100 },
      { event: "sync_progress", kwargs: { status: "proton_meta" }, timestamp: 101 },
      { event: "shortcut_reconcile_complete", kwargs: { added: 3, removed: 1 }, timestamp: 102 },
    ];
    mockCall.mockResolvedValue(buffered);

    await vi.advanceTimersByTimeAsync(POLL_SLOW_MS);

    // Primed past — the prior session's sync must not re-animate the UI.
    expect(started).not.toHaveBeenCalled();
    expect(progress).not.toHaveBeenCalled();
    expect(reconcile).not.toHaveBeenCalled();
  });

  it("DOES dispatch non-sync state events on the first poll (suppression is targeted)", async () => {
    const { EventBusClient } = await import("./event-bus-client");
    const auth = vi.fn();
    EventBusClient.subscribe("store_auth_complete", auth);

    mockCall.mockResolvedValue([
      { event: "store_auth_complete", kwargs: { store: "gog" }, timestamp: 100 },
    ]);

    await vi.advanceTimersByTimeAsync(POLL_SLOW_MS);

    // Auth state must still restore on boot — only sync lifecycle + imperative
    // events are primed past.
    expect(auth).toHaveBeenCalledTimes(1);
    expect(auth).toHaveBeenCalledWith({ store: "gog" });
  });

  it("dispatches sync events that arrive LIVE after the first-poll prime", async () => {
    const { EventBusClient } = await import("./event-bus-client");
    const started = vi.fn();
    EventBusClient.subscribe("sync_started", started);

    // First poll: a buffered (stale) sync_started — primed past.
    mockCall.mockResolvedValueOnce([
      { event: "sync_started", kwargs: { scope: "all" }, timestamp: 100 },
    ]);
    await vi.advanceTimersByTimeAsync(POLL_SLOW_MS);
    expect(started).not.toHaveBeenCalled();

    // Second poll (now primed): a NEW sync_started past the watermark fires.
    mockCall.mockResolvedValueOnce([
      { event: "sync_started", kwargs: { scope: "all" }, timestamp: 200 },
    ]);
    await vi.advanceTimersByTimeAsync(POLL_FAST_MS);
    expect(started).toHaveBeenCalledTimes(1);
  });
});

describe("EventBusClient server-side watermark", () => {
  it("sends its watermark so the backend can filter, and advances it", async () => {
    const { EventBusClient } = await import("./event-bus-client");
    EventBusClient.subscribe("store_auth_complete", vi.fn());

    // First poll has no watermark yet, so it must ask for everything —
    // the priming pass depends on receiving the whole backlog.
    mockCall.mockResolvedValueOnce([
      { event: "store_auth_complete", kwargs: { store: "gog" }, timestamp: 100 },
    ]);
    await vi.advanceTimersByTimeAsync(POLL_SLOW_MS);
    expect(mockCall.mock.calls[0][2]).toBe(0);

    // Second poll carries the highest timestamp seen. Without this the
    // backend re-serialises its entire replay buffer on every poll, twice
    // a second, for the life of the process.
    mockCall.mockResolvedValueOnce([]);
    await vi.advanceTimersByTimeAsync(POLL_FAST_MS);
    expect(mockCall.mock.calls[1][2]).toBe(100);
  });
});

describe("EventBusClient poll-timer duplication", () => {
  it("arms exactly one timer when a subscriber leaves and rejoins mid-poll", async () => {
    const { EventBusClient } = await import("./event-bus-client");

    // Hold the in-flight poll open so the unsubscribe/resubscribe below
    // lands while `pollOnce` is still awaiting.
    let release: (v: unknown) => void = () => {};
    mockCall.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );

    const unsubscribe = EventBusClient.subscribe("sync_started", vi.fn());
    await vi.advanceTimersByTimeAsync(POLL_SLOW_MS);

    // Last subscriber leaves (stopPolling) then a new one joins
    // (ensurePolling arms a timer) — all while the poll is in flight.
    unsubscribe();
    EventBusClient.subscribe("sync_started", vi.fn());

    // Now let the in-flight poll finish; its `finally` also wants to arm.
    mockCall.mockResolvedValue([]);
    release([]);
    await vi.advanceTimersByTimeAsync(0);

    // If both survived there would be two independent loops, and the poll
    // rate would double again on every repeat of this sequence.
    expect(vi.getTimerCount()).toBe(1);
  });
});
