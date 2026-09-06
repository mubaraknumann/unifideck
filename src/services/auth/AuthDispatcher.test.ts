// @vitest-environment jsdom
/**
 * Regression: a successful auth must trigger request_auth_sync.
 *
 * The backend's `request_auth_sync` (SyncService.request_auth_sync)
 * exists specifically so a store becomes available for sync the
 * moment login completes, instead of only after the next restart
 * forces a fresh boot-time availability check. Its own docstring says
 * "Called by AuthDispatcher after store auth" — but nothing actually
 * called it, so a sync run right after signing in silently skipped
 * the just-authenticated store. These tests pin that AuthDispatcher
 * calls it on success and does NOT call it on failure.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const mockCall = vi.fn();
vi.mock("@decky/api", () => ({
  call: (...args: unknown[]) => mockCall(...args),
}));

// Reached transitively: AuthDispatcher → prepare-sync → stores/sync-store,
// which imports `showModal`. The real `@decky/ui` runs `initModuleCache()` at
// import time against `window.webpackChunksteamui`, which does not exist
// outside the Steam webview, so importing it here throws before any test runs.
vi.mock("@decky/ui", () => ({ showModal: vi.fn() }));

// The post-login sync awaits this before the RPC. Its three steps talk to
// SteamClient, which is absent here, so the real one rejects and the `.then`
// holding `request_auth_sync` never runs. What is under test is that the
// dispatcher fires the RPC *after* preparing, not what preparing does.
vi.mock("../../lib/steam-bridge/prepare-sync", () => ({
  prepareForSync: vi.fn(() => Promise.resolve(undefined)),
}));

type Handler = (payload: unknown) => void;
const subscribers = new Map<string, Set<Handler>>();
vi.mock("../../api/event-bus-client", () => ({
  EventBusClient: {
    bumpToFast: vi.fn(),
    subscribe: (name: string, handler: Handler) => {
      const set = subscribers.get(name) ?? new Set<Handler>();
      set.add(handler);
      subscribers.set(name, set);
      return () => set.delete(handler);
    },
  },
}));

vi.mock("../../api/useRPC", () => ({
  unwrapRpcEnvelope: (raw: unknown) => raw,
}));

vi.mock("../../api/rpc-routes", () => ({
  rpcRoutes: {
    storeAuth: "store_auth",
    requestAuthSync: "request_auth_sync",
    checkStoreStatus: "check_store_status",
  },
}));

vi.mock("../../types/events", () => ({
  Events: {
    STORE_AUTH_COMPLETE: "store_auth_complete",
    STORE_AUTH_FAILED: "store_auth_failed",
  },
}));

const shortcutLaunched = { success: true };
vi.mock("../../utils/authShortcutLaunch", () => ({
  launchEpicAuthViaShortcut: vi.fn(() => Promise.resolve(shortcutLaunched)),
  launchGogAuthViaShortcut: vi.fn(() => Promise.resolve(shortcutLaunched)),
  launchAmazonAuthViaShortcut: vi.fn(() => Promise.resolve(shortcutLaunched)),
  launchMicrosoftAuthViaShortcut: vi.fn(() => Promise.resolve(shortcutLaunched)),
}));

vi.mock("../../utils/ubisoftShortcutLaunch", () => ({
  launchUbisoftAuthViaShortcut: vi.fn(() => Promise.resolve(shortcutLaunched)),
}));

// Battle.net reports the appid it launched, which is what lets the
// dispatcher notice the sign-in ended without a backend event.
const BNET_APP_ID = 4005795639;
vi.mock("../../utils/battlenetShortcutLaunch", () => ({
  launchBattlenetAuthViaShortcut: vi.fn(() =>
    Promise.resolve({ success: true, app_id: BNET_APP_ID }),
  ),
}));

/** Steam's app-lifetime callbacks, keyed by nothing — we drive them all. */
let lifetimeHandlers: Array<(n: { unAppID: number; bRunning: boolean }) => void> = [];

function installSteamClient(): void {
  lifetimeHandlers = [];
  (window as unknown as { SteamClient?: unknown }).SteamClient = {
    GameSessions: {
      RegisterForAppLifetimeNotifications: (
        cb: (n: { unAppID: number; bRunning: boolean }) => void,
      ) => {
        lifetimeHandlers.push(cb);
        return {
          unregister: () => {
            lifetimeHandlers = lifetimeHandlers.filter((h) => h !== cb);
          },
        };
      },
    },
  };
}

function appLifetime(appId: number, running: boolean): void {
  for (const h of [...lifetimeHandlers]) h({ unAppID: appId, bRunning: running });
}

function emit(event: string, payload: unknown): void {
  for (const handler of subscribers.get(event) ?? []) handler(payload);
}

describe("AuthDispatcher", () => {
  beforeEach(() => {
    vi.resetModules();
    mockCall.mockReset();
    mockCall.mockResolvedValue({ success: true }); // default: any later RPC (requestAuthSync) resolves fine
    subscribers.clear();
  });

  it("calls request_auth_sync when auth completes successfully", async () => {
    // store_auth("start") resolves to the slow path (false) so the flow
    // waits for the STORE_AUTH_COMPLETE event rather than the fast path.
    mockCall.mockResolvedValueOnce({ success: false });
    const { AuthDispatcher } = await import("./AuthDispatcher");

    const promise = AuthDispatcher.start("microsoft");
    await Promise.resolve(); // let kickAndLaunch's microtasks settle
    await Promise.resolve();
    emit("store_auth_complete", { store: "microsoft" });
    const result = await promise;

    expect(result.success).toBe(true);
    expect(mockCall).toHaveBeenCalledWith("request_auth_sync", "microsoft");
  });

  it("does not call request_auth_sync when auth fails", async () => {
    mockCall.mockResolvedValueOnce({ success: false });
    const { AuthDispatcher } = await import("./AuthDispatcher");

    const promise = AuthDispatcher.start("epic");
    await Promise.resolve();
    await Promise.resolve();
    emit("store_auth_failed", { store: "epic", error: "denied" });
    const result = await promise;

    expect(result.success).toBe(false);
    expect(mockCall).not.toHaveBeenCalledWith("request_auth_sync", "epic");
  });
});

/**
 * Regression: a sign-in that ends without a terminal event must not wedge
 * the button.
 *
 * `inflight` is a module singleton, so a promise that never settles is
 * returned to every later press — no RPC, no shortcut launch, a dead
 * button. Battle.net emitted no `STORE_AUTH_*` event at all, so a login
 * that ended on a rejected password left the store unusable until Steam
 * was restarted (which is simply what rebuilds this singleton). Reported
 * from a tester's device.
 */
describe("AuthDispatcher does not wedge on a flow that never settles", () => {
  beforeEach(() => {
    vi.resetModules();
    mockCall.mockReset();
    // Always the slow path: every start waits for an event.
    mockCall.mockResolvedValue({ success: false });
    subscribers.clear();
    installSteamClient();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** Number of `store_auth` kicks made so far. */
  const kicks = (): number => mockCall.mock.calls.filter((c) => c[0] === "store_auth").length;

  it("deduplicates a genuine double-click", async () => {
    vi.useFakeTimers();
    const { AuthDispatcher } = await import("./AuthDispatcher");

    void AuthDispatcher.start("battlenet");
    await vi.advanceTimersByTimeAsync(10);
    void AuthDispatcher.start("battlenet");
    await vi.advanceTimersByTimeAsync(10);

    expect(kicks()).toBe(1);
  });

  it("starts a fresh flow when the user presses again later", async () => {
    vi.useFakeTimers();
    const { AuthDispatcher } = await import("./AuthDispatcher");

    const first = AuthDispatcher.start("battlenet");
    await vi.advanceTimersByTimeAsync(10);
    expect(kicks()).toBe(1);

    // Past the double-click window: this is a deliberate retry, and it
    // must reach the backend rather than be handed the stale promise.
    await vi.advanceTimersByTimeAsync(30_000);
    void AuthDispatcher.start("battlenet");
    await vi.advanceTimersByTimeAsync(10);

    expect(kicks()).toBe(2);
    // The abandoned flow settles rather than leaking.
    await expect(first).resolves.toMatchObject({ success: false });
  });

  it("settles when the auth app exits without a backend verdict", async () => {
    vi.useFakeTimers();
    const { AuthDispatcher } = await import("./AuthDispatcher");

    const promise = AuthDispatcher.start("battlenet");
    await vi.advanceTimersByTimeAsync(10);

    // The client came up, the user got it wrong, the client closed.
    appLifetime(BNET_APP_ID, true);
    appLifetime(BNET_APP_ID, false);
    await vi.advanceTimersByTimeAsync(25_000);

    const result = await promise;
    expect(result.success).toBe(false);
    expect(result.error).toMatch(/closed before it completed/);
    expect(mockCall).not.toHaveBeenCalledWith("request_auth_sync", "battlenet");
  });

  /**
   * Regression: a completed sign-in reported as a failure, which sent the
   * user straight back into another launch.
   *
   * A wrapper store writes its session as the client shuts down, and the
   * backend only clears its signed-out marker after the post-capture hook
   * runs, so the terminal event can land after the exit grace has already
   * expired. Failing on the timer alone made a successful Battle.net
   * sign-in relaunch the client. Reported from end-to-end testing.
   */
  it("asks the backend before calling an exited sign-in failed", async () => {
    vi.useFakeTimers();
    // store_auth kick → slow path; check_store_status → store IS connected.
    mockCall.mockImplementation((route: string) =>
      route === "check_store_status"
        ? Promise.resolve([{ store_id: "battlenet", available: true }])
        : Promise.resolve({ success: false }),
    );
    const { AuthDispatcher } = await import("./AuthDispatcher");

    const promise = AuthDispatcher.start("battlenet");
    await vi.advanceTimersByTimeAsync(10);

    appLifetime(BNET_APP_ID, true);
    appLifetime(BNET_APP_ID, false);
    await vi.advanceTimersByTimeAsync(25_000);

    const result = await promise;
    expect(result.success).toBe(true);
    expect(mockCall).toHaveBeenCalledWith("check_store_status");
    // Success must still drive the post-auth sync.
    expect(mockCall).toHaveBeenCalledWith("request_auth_sync", "battlenet");
  });

  it("still fails when the backend agrees the store is not connected", async () => {
    vi.useFakeTimers();
    mockCall.mockImplementation((route: string) =>
      route === "check_store_status"
        ? Promise.resolve([{ store_id: "battlenet", available: false }])
        : Promise.resolve({ success: false }),
    );
    const { AuthDispatcher } = await import("./AuthDispatcher");

    const promise = AuthDispatcher.start("battlenet");
    await vi.advanceTimersByTimeAsync(10);
    appLifetime(BNET_APP_ID, true);
    appLifetime(BNET_APP_ID, false);
    await vi.advanceTimersByTimeAsync(25_000);

    const result = await promise;
    expect(result.success).toBe(false);
    expect(result.error).toMatch(/closed before it completed/);
  });

  it("keeps the failure verdict when the status probe itself throws", async () => {
    vi.useFakeTimers();
    mockCall.mockImplementation((route: string) =>
      route === "check_store_status"
        ? Promise.reject(new Error("backend gone"))
        : Promise.resolve({ success: false }),
    );
    const { AuthDispatcher } = await import("./AuthDispatcher");

    const promise = AuthDispatcher.start("battlenet");
    await vi.advanceTimersByTimeAsync(10);
    appLifetime(BNET_APP_ID, true);
    appLifetime(BNET_APP_ID, false);
    await vi.advanceTimersByTimeAsync(25_000);

    await expect(promise).resolves.toMatchObject({ success: false });
  });

  it("lets a late STORE_AUTH_COMPLETE win over the exit grace", async () => {
    vi.useFakeTimers();
    const { AuthDispatcher } = await import("./AuthDispatcher");

    const promise = AuthDispatcher.start("battlenet");
    await vi.advanceTimersByTimeAsync(10);

    appLifetime(BNET_APP_ID, true);
    appLifetime(BNET_APP_ID, false);
    // The client flushes its token as it shuts down, so the backend's
    // capture routinely lands after the app has already stopped.
    await vi.advanceTimersByTimeAsync(2000);
    emit("store_auth_complete", { store: "battlenet" });

    const result = await promise;
    expect(result.success).toBe(true);
  });

  it("ignores lifetime notifications for a different app", async () => {
    vi.useFakeTimers();
    const { AuthDispatcher } = await import("./AuthDispatcher");

    const promise = AuthDispatcher.start("battlenet");
    await vi.advanceTimersByTimeAsync(10);

    appLifetime(BNET_APP_ID + 1, true);
    appLifetime(BNET_APP_ID + 1, false);
    await vi.advanceTimersByTimeAsync(25_000);

    let settled = false;
    void promise.then(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(settled).toBe(false);

    // Keep the 10-minute ceiling from rejecting into an unhandled error.
    emit("store_auth_failed", { store: "battlenet", error: "done" });
    await expect(promise).resolves.toMatchObject({ success: false });
  });
});
