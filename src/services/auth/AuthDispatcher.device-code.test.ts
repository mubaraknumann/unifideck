// @vitest-environment jsdom
/**
 * The Microsoft device-code sign-in outlives its window.
 *
 * The backend (`MicrosoftDeviceAuth`) polls Microsoft until the user presses
 * "Allow access" or the code expires, then emits the terminal event. So for
 * this flow the auth window closing is NOT a failure: the dispatcher keeps
 * waiting, the QAM keeps the code (`pending-signin-store`) and can reopen
 * the window or cancel. Pinned here:
 *
 * - the code is published as soon as `store_auth("start")` returns it;
 * - window close → still pending (windowOpen=false), no verdict;
 * - a late STORE_AUTH_COMPLETE / STORE_AUTH_FAILED settles the flow and
 *   clears the pending entry;
 * - cancel → `store_auth("cancel")` + a silent "cancelled" result;
 * - reopen → relaunch + windowOpen=true.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { parseDeviceCodeStart } from "./device-code";

const mockCall = vi.fn();
vi.mock("@decky/api", () => ({
  call: (...args: unknown[]) => mockCall(...args),
}));
vi.mock("@decky/ui", () => ({ showModal: vi.fn() }));
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
vi.mock("./store-status", () => ({
  storeReportsConnected: vi.fn(() => Promise.resolve(false)),
}));

const MS_APP_ID = 3001;
const REOPENED_APP_ID = 3002;
const launchMicrosoft = vi.fn();
vi.mock("../../utils/authShortcutLaunch", () => ({
  launchEpicAuthViaShortcut: vi.fn(),
  launchGogAuthViaShortcut: vi.fn(),
  launchAmazonAuthViaShortcut: vi.fn(),
  launchItchAuthViaShortcut: vi.fn(),
  launchMicrosoftAuthViaShortcut: () => launchMicrosoft(),
}));
vi.mock("../../utils/ubisoftShortcutLaunch", () => ({
  launchUbisoftAuthViaShortcut: vi.fn(),
}));
vi.mock("../../utils/battlenetShortcutLaunch", () => ({
  launchBattlenetAuthViaShortcut: vi.fn(),
}));

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
/** The auth window ran and closed (`watchAppStopped` needs to see it run). */
function windowClosed(appId: number): void {
  appLifetime(appId, true);
  appLifetime(appId, false);
}
function emit(event: string, payload: unknown): void {
  for (const handler of subscribers.get(event) ?? []) handler(payload);
}

const DEVICE_START = {
  success: true,
  store: "microsoft",
  metadata: {
    pending: true,
    flow: "device_code",
    user_code: "9W2495MG",
    verification_uri: "https://www.microsoft.com/link",
    expires_in: 900,
  },
};

async function flush(): Promise<void> {
  for (let i = 0; i < 6; i++) await Promise.resolve();
}

async function startDeviceFlow() {
  const { AuthDispatcher } = await import("./AuthDispatcher");
  const { pendingSignIns } = await import("../../stores/pending-signin-store");
  const promise = AuthDispatcher.start("microsoft");
  await flush();
  return { AuthDispatcher, pendingSignIns, promise };
}

describe("AuthDispatcher — device-code sign-in", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
    mockCall.mockReset();
    mockCall.mockImplementation((route: string, _store: string, action?: string) =>
      Promise.resolve(
        route === "store_auth" && action === "start" ? DEVICE_START : { success: true },
      ),
    );
    launchMicrosoft.mockReset();
    launchMicrosoft
      .mockResolvedValueOnce({ success: true, app_id: MS_APP_ID })
      .mockResolvedValue({ success: true, app_id: REOPENED_APP_ID });
    subscribers.clear();
    installSteamClient();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("publishes the code as soon as the backend hands it out", async () => {
    const { pendingSignIns } = await startDeviceFlow();
    const pending = pendingSignIns.get("microsoft");
    expect(pending?.userCode).toBe("9W2495MG");
    expect(pending?.windowOpen).toBe(true);
  });

  it("keeps waiting when the window closes, then succeeds on the backend's event", async () => {
    const { pendingSignIns, promise } = await startDeviceFlow();
    windowClosed(MS_APP_ID);
    await vi.advanceTimersByTimeAsync(25_000); // past the app-stopped grace
    expect(pendingSignIns.get("microsoft")?.windowOpen).toBe(false);

    let settled = false;
    void promise.then(() => (settled = true));
    await flush();
    expect(settled).toBe(false);

    emit("store_auth_complete", { store: "microsoft" });
    const result = await promise;
    expect(result.success).toBe(true);
    expect(pendingSignIns.get("microsoft")).toBeUndefined();
  });

  it("reports the backend's named failure after the window closed", async () => {
    const { promise } = await startDeviceFlow();
    windowClosed(MS_APP_ID);
    await vi.advanceTimersByTimeAsync(25_000);
    emit("store_auth_failed", { store: "microsoft", error: "device_code_expired" });
    const result = await promise;
    expect(result).toMatchObject({ success: false, error: "device_code_expired" });
  });

  it("cancel tells the backend and settles silently", async () => {
    const { AuthDispatcher, pendingSignIns, promise } = await startDeviceFlow();
    await AuthDispatcher.cancel("microsoft");
    const result = await promise;
    expect(mockCall).toHaveBeenCalledWith("store_auth", "microsoft", "cancel");
    expect(result).toMatchObject({ success: false, error: "cancelled" });
    expect(pendingSignIns.get("microsoft")).toBeUndefined();
  });

  it("reopen relaunches the window and watches the new one", async () => {
    const { AuthDispatcher, pendingSignIns } = await startDeviceFlow();
    windowClosed(MS_APP_ID);
    await flush();
    expect(await AuthDispatcher.reopenWindow("microsoft")).toBe(true);
    expect(launchMicrosoft).toHaveBeenCalledTimes(2);
    expect(pendingSignIns.get("microsoft")?.windowOpen).toBe(true);
    windowClosed(REOPENED_APP_ID);
    await flush();
    expect(pendingSignIns.get("microsoft")?.windowOpen).toBe(false);
  });

  it("gives up after the code's lifetime if no event ever arrives", async () => {
    const { promise } = await startDeviceFlow();
    await vi.advanceTimersByTimeAsync(900_000 + 61_000);
    const result = await promise;
    expect(result).toMatchObject({ success: false, error: "device_code_expired" });
  });
});

describe("parseDeviceCodeStart", () => {
  it("reads the backend's metadata", () => {
    expect(parseDeviceCodeStart(DEVICE_START)).toEqual({
      userCode: "9W2495MG",
      verificationUri: "https://www.microsoft.com/link",
      expiresInSec: 900,
    });
  });

  it("ignores ordinary sign-ins", () => {
    expect(parseDeviceCodeStart({ success: true, metadata: { pending: true } })).toBeNull();
    expect(parseDeviceCodeStart(null)).toBeNull();
  });

  it("defaults what the backend left out", () => {
    expect(parseDeviceCodeStart({ metadata: { flow: "device_code", user_code: "X" } })).toEqual({
      userCode: "X",
      verificationUri: "https://www.microsoft.com/link",
      expiresInSec: 900,
    });
  });
});
