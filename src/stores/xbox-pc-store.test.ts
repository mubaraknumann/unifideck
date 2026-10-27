/**
 * The Xbox PC status store: what the game page and the QAM read.
 *
 * The cases that matter are the ones that would mislead the user:
 *   - a backend that never answers must read as an error, not as "off";
 *   - a replayed or malformed event must not wipe a good status;
 *   - `getSnapshot` must keep its identity when nothing changed, or
 *     `useSyncExternalStore` re-renders forever;
 *   - an envelope error code must reach the caller, not turn into null.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { envelope } from "../test-support/rpc-envelope";

const api = vi.hoisted(() => ({
  call: vi.fn(),
  toast: vi.fn(),
}));
const bus = vi.hoisted(() => ({
  handlers: new Map<string, (p: Record<string, unknown>) => void>(),
  unsubscribed: 0,
}));

vi.mock("@decky/api", () => ({
  call: api.call,
  toaster: { toast: api.toast },
}));
vi.mock("i18next", () => ({
  default: { t: (k: string) => k },
}));
vi.mock("../api/event-bus-client", () => ({
  EventBusClient: {
    subscribe: (name: string, fn: (p: Record<string, unknown>) => void) => {
      bus.handlers.set(name, fn);
      return () => {
        bus.unsubscribed += 1;
        bus.handlers.delete(name);
      };
    },
  },
}));

import { _createXboxPcStore, callXboxPc, isXboxPcStatus, STATUS_TIMEOUT_MS } from "./xbox-pc-store";

function status(over: Record<string, unknown> = {}) {
  return {
    available: true,
    enabled: false,
    notice_version: 1,
    notice_ack_version: 0,
    xcloud_signed_in: true,
    runtime: { state: "missing", version: null, source_dir: "/src", degraded: [] },
    account: { state: "unknown" },
    library: {
      installable: 0,
      not_supported: 0,
      choice: 0,
      skipped: 0,
      stale: false,
      last_read: null,
    },
    ...over,
  };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  api.call.mockReset();
  api.toast.mockReset();
  bus.handlers.clear();
  bus.unsubscribed = 0;
});

afterEach(() => {
  vi.useRealTimers();
});

describe("xbox-pc-store", () => {
  it("loads the status at start", async () => {
    api.call.mockResolvedValue(envelope(status({ enabled: true })));
    const store = _createXboxPcStore();
    store.start();
    await flush();
    const snap = store.getSnapshot();
    expect(snap.loading).toBe(false);
    expect(snap.error).toBeNull();
    expect(snap.status?.enabled).toBe(true);
    expect(api.call).toHaveBeenCalledWith("get_xbox_pc_status");
    store.stop();
  });

  it("reports status_unavailable, not 'off', when the call fails", async () => {
    api.call.mockResolvedValue(envelope(null, false, "boom"));
    const store = _createXboxPcStore();
    store.start();
    await flush();
    expect(store.getSnapshot()).toEqual({
      status: null,
      loading: false,
      error: "status_unavailable",
    });
    store.stop();
  });

  it("times out to status_unavailable when the call never returns", async () => {
    vi.useFakeTimers();
    api.call.mockReturnValue(new Promise(() => {}));
    const store = _createXboxPcStore();
    store.start();
    expect(store.getSnapshot().loading).toBe(true);
    vi.advanceTimersByTime(STATUS_TIMEOUT_MS + 1);
    expect(store.getSnapshot().error).toBe("status_unavailable");
    store.stop();
  });

  it("replaces the snapshot from the event and clears an error", async () => {
    api.call.mockResolvedValue(envelope(null, false, "boom"));
    const store = _createXboxPcStore();
    store.start();
    await flush();
    bus.handlers.get("xbox_pc_state_changed")?.(status({ enabled: true }));
    const snap = store.getSnapshot();
    expect(snap.error).toBeNull();
    expect(snap.status?.enabled).toBe(true);
    store.stop();
  });

  it("ignores a malformed event instead of wiping a good status", async () => {
    api.call.mockResolvedValue(envelope(status({ enabled: true })));
    const store = _createXboxPcStore();
    store.start();
    await flush();
    const before = store.getSnapshot();
    bus.handlers.get("xbox_pc_state_changed")?.({ enabled: false });
    expect(store.getSnapshot()).toBe(before);
    store.stop();
  });

  it("keeps a good status when a later fetch fails", async () => {
    api.call.mockResolvedValueOnce(envelope(status({ enabled: true })));
    const store = _createXboxPcStore();
    store.start();
    await flush();
    api.call.mockResolvedValueOnce(envelope(null, false, "boom"));
    await store.refetch();
    expect(store.getSnapshot().status?.enabled).toBe(true);
    expect(store.getSnapshot().error).toBeNull();
    store.stop();
  });

  it("keeps the snapshot identity when an identical status arrives", async () => {
    api.call.mockResolvedValue(envelope(status()));
    const store = _createXboxPcStore();
    const listener = vi.fn();
    store.subscribe(listener);
    store.start();
    await flush();
    const first = store.getSnapshot();
    const calls = listener.mock.calls.length;
    store.apply(status());
    expect(store.getSnapshot()).toBe(first);
    expect(listener.mock.calls.length).toBe(calls);
    store.stop();
  });

  it("stop() drops the event subscription", () => {
    api.call.mockReturnValue(new Promise(() => {}));
    const store = _createXboxPcStore();
    store.start();
    store.stop();
    expect(bus.unsubscribed).toBe(1);
    expect(bus.handlers.size).toBe(0);
  });

  it("hands setEnabled's refusal code back to the caller", async () => {
    api.call.mockResolvedValue(envelope(null, false, "notice_not_acknowledged"));
    const store = _createXboxPcStore();
    const r = await store.setEnabled(true, 1);
    expect(r).toEqual({ ok: false, error: "notice_not_acknowledged" });
    expect(api.call).toHaveBeenCalledWith("set_xbox_pc_enabled", true, 1);
  });

  it("applies the status setEnabled answers with", async () => {
    api.call.mockResolvedValue(envelope(status({ enabled: true })));
    const store = _createXboxPcStore();
    const r = await store.setEnabled(true, 1);
    expect(r.ok).toBe(true);
    expect(store.getSnapshot().status?.enabled).toBe(true);
  });

  it("turns a not-started runtime install into a refusal", async () => {
    api.call.mockResolvedValue(envelope({ started: false, reason: "source_missing:/x" }));
    const r = await _createXboxPcStore().installRuntime();
    expect(r).toEqual({ ok: false, error: "source_missing:/x" });
  });

  it("stays quiet when a full runtime installs: the sign-in toast follows", async () => {
    const store = _createXboxPcStore();
    store.apply(status({ runtime: { state: "installing", source_dir: "/s", degraded: [] } }));
    store.apply(status({ runtime: { state: "installed", source_dir: "/s", degraded: [] } }));
    expect(api.toast).not.toHaveBeenCalled();
  });

  it("toasts what a degraded runtime cannot do (rule 8)", async () => {
    const store = _createXboxPcStore();
    store.apply(status({ runtime: { state: "installing", source_dir: "/s", degraded: [] } }));
    store.apply(
      status({
        runtime: { state: "installed", source_dir: "/s", degraded: ["cloud_saves", "other_x"] },
      }),
    );
    expect(api.toast).toHaveBeenCalledTimes(1);
    expect(api.toast.mock.calls[0][0]).toMatchObject({
      title: "toasts.xboxPcRuntimeInstalled",
      body: "xboxPc.runtime.degradedCloudSaves xboxPc.runtime.degradedOther",
    });
  });

  it("toasts once when Microsoft starts confirming the device", async () => {
    const store = _createXboxPcStore();
    store.apply(status({ account: { state: "signing_in" } }));
    expect(api.toast).not.toHaveBeenCalled();
    store.apply(status({ account: { state: "signing_in", step: "device" } }));
    store.apply(status({ account: { state: "signing_in", step: "device" }, enabled: true }));
    expect(api.toast).toHaveBeenCalledTimes(1);
    expect(api.toast.mock.calls[0][0]).toMatchObject({
      title: "toasts.xboxPcConfirmingDevice",
      body: "toasts.xboxPcConfirmingDeviceBody",
    });
  });

  it("toasts a failed component install with its cause", async () => {
    const store = _createXboxPcStore();
    store.apply(status({ runtime: { state: "installing", source_dir: "/s", degraded: [] } }));
    store.apply(
      status({
        runtime: { state: "failed", error: "source_ambiguous", source_dir: "/s", degraded: [] },
      }),
    );
    expect(api.toast.mock.calls[0][0]).toMatchObject({
      title: "toasts.xboxPcRuntimeFailed",
      body: "xboxPc.runtime.errors.sourceAmbiguous",
    });
  });
});

describe("callXboxPc / isXboxPcStatus", () => {
  it("returns a rejected call as an error, never throws", async () => {
    api.call.mockRejectedValue(new Error("socket closed"));
    expect(await callXboxPc("get_xbox_pc_status")).toEqual({
      ok: false,
      error: "socket closed",
    });
  });

  it("rejects payloads missing the fields the UI reads", () => {
    expect(isXboxPcStatus(status())).toBe(true);
    expect(isXboxPcStatus({ available: true, enabled: true })).toBe(false);
    expect(isXboxPcStatus(null)).toBe(false);
  });
});
