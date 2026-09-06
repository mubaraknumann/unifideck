/**
 * Regression: connecting GameVault must trigger the post-login sync.
 *
 * Every other store reaches `AuthDispatcher`, whose `onResolved` fires
 * `prepareForSync()` then `request_auth_sync` so a freshly signed-in store
 * populates itself instead of waiting for the user to press Sync. GameVault
 * is the one store whose sign-in is a modal chain rather than a browser OAuth
 * or an auth shortcut, so `connect` short-circuits before the dispatcher is
 * ever asked — and the sync kick was lost with it. A tester connected a
 * remote GameVault server and got an empty library until they synced by hand.
 *
 * `AuthDispatcher.test.ts` pins the same behaviour for the dispatcher path.
 * These tests pin it for the branch that bypasses it, because the two copies
 * are kept in step by hand.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const mockCall = vi.hoisted(() => vi.fn((..._args: unknown[]) => Promise.resolve(undefined)));
vi.mock("@decky/api", () => ({
  call: (...args: unknown[]) => mockCall(...args),
}));

vi.mock("@decky/ui", () => ({ showModal: vi.fn() }));

const mockPrepareForSync = vi.hoisted(() => vi.fn(() => Promise.resolve(undefined)));
vi.mock("../lib/steam-bridge/prepare-sync", () => ({
  prepareForSync: mockPrepareForSync,
}));

vi.mock("../api/rpc-routes", () => ({
  rpcRoutes: { requestAuthSync: "request_auth_sync" },
}));

const mockConnectGameVault = vi.hoisted(() => vi.fn());
vi.mock("../lib/gamevault-connect", () => ({
  connectGameVault: mockConnectGameVault,
}));

const notifyConnected = vi.hoisted(() => vi.fn());
vi.mock("../contexts/AuthContext", () => ({
  useAuth: () => ({ statuses: {}, notifyConnected, notifyDisconnected: vi.fn() }),
}));

vi.mock("../contexts/StoreContext", () => ({
  useStores: () => ({ stores: [{ name: "gamevault" }] }),
}));

vi.mock("./useToast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

// Imported by the hook for the non-GameVault branch only; never exercised
// here, but the module has to resolve.
vi.mock("../services/auth/AuthDispatcher", () => ({
  AuthDispatcher: { start: vi.fn(), logout: vi.fn() },
}));

vi.mock("../components/modals/ChromiumInstallModal", () => ({
  ChromiumInstallModal: () => null,
}));

import { useStoreAuth } from "./useStoreAuth";

/** Let the fire-and-forget promise chain settle before asserting. */
async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

describe("useStoreAuth — GameVault post-connect sync", () => {
  beforeEach(() => {
    mockCall.mockClear();
    mockPrepareForSync.mockClear();
    notifyConnected.mockClear();
    mockCall.mockImplementation(() => Promise.resolve(undefined));
  });

  it("prepares for sync, then requests the auth sync, exactly once", async () => {
    mockConnectGameVault.mockResolvedValue({ success: true });

    const { connect } = useStoreAuth("gamevault");
    await connect();
    await flush();

    expect(mockPrepareForSync).toHaveBeenCalledTimes(1);
    expect(mockCall).toHaveBeenCalledTimes(1);
    expect(mockCall).toHaveBeenCalledWith("request_auth_sync", "gamevault");
    // Order matters: a bare requestAuthSync is the exact gap that made the
    // automatic sync behave differently from a user-pressed one.
    expect(mockPrepareForSync.mock.invocationCallOrder[0]).toBeLessThan(
      mockCall.mock.invocationCallOrder[0],
    );
  });

  it("does not sync when the connect failed", async () => {
    mockConnectGameVault.mockResolvedValue({
      success: false,
      error: "bad_credentials",
    });

    const { connect } = useStoreAuth("gamevault");
    await connect();
    await flush();

    expect(mockPrepareForSync).not.toHaveBeenCalled();
    expect(mockCall).not.toHaveBeenCalled();
  });

  it("does not sync when the user dismissed the modal chain", async () => {
    mockConnectGameVault.mockResolvedValue(null);

    const { connect } = useStoreAuth("gamevault");
    expect(await connect()).toBeNull();
    await flush();

    expect(mockPrepareForSync).not.toHaveBeenCalled();
    expect(mockCall).not.toHaveBeenCalled();
  });

  it("survives a rejecting sync RPC — connect still resolves", async () => {
    mockConnectGameVault.mockResolvedValue({ success: true });
    mockCall.mockImplementation(() => Promise.reject(new Error("backend down")));
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    const { connect } = useStoreAuth("gamevault");
    // The assertion is that this does not reject: the sync is fire-and-forget,
    // so a dead backend must not turn a successful sign-in into a failure.
    await expect(connect()).resolves.toEqual({ success: true });
    await flush();

    expect(notifyConnected).toHaveBeenCalledWith("gamevault");
    expect(errorSpy).toHaveBeenCalled();
    errorSpy.mockRestore();
  });
});
