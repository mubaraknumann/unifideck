/**
 * linkXboxPc: one Microsoft sign-in brings Xbox PC up by itself.
 *
 * Pinned: nothing happens with the toggle off or Microsoft disconnected;
 * missing components install first, then the sign-in runs; a linked account
 * is not signed in again; `signIn: false` (boot) never opens a sign-in;
 * concurrent callers share one run.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

type Status = {
  enabled: boolean;
  xcloud_signed_in: boolean;
  runtime: { state: string };
  account: { state: string };
};

const h = vi.hoisted(() => {
  let status: Status | null = null;
  const listeners = new Set<() => void>();
  const set = (next: Status | null): void => {
    status = next;
    for (const fn of [...listeners]) fn();
  };
  return {
    set,
    store: {
      getSnapshot: () => ({ status, loading: false, error: null }),
      subscribe: (fn: () => void) => {
        listeners.add(fn);
        return () => listeners.delete(fn);
      },
      refetch: vi.fn(async () => {}),
      refresh: vi.fn(async () => {}),
      installRuntime: vi.fn(),
    },
    signIn: vi.fn(async () => true),
    toastError: vi.fn(),
  };
});

vi.mock("../../stores/xbox-pc-store", () => ({ xboxPcStore: h.store }));
vi.mock("../auth/xbox-pc-sign-in", () => ({ signInXboxPc: h.signIn }));
vi.mock("./xbox-pc-actions", () => ({ toastXboxPcError: h.toastError }));

import { linkXboxPc } from "./xbox-pc-link";

function status(over: Partial<Status> = {}): Status {
  return {
    enabled: true,
    xcloud_signed_in: true,
    runtime: { state: "installed" },
    account: { state: "signed_out" },
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  h.set(null);
});

describe("linkXboxPc", () => {
  it("does nothing with the toggle off; no sign-in without Microsoft", async () => {
    h.set(status({ enabled: false }));
    expect(await linkXboxPc()).toBe(false);
    h.set(status({ xcloud_signed_in: false }));
    expect(await linkXboxPc()).toBe(false);
    expect(h.store.installRuntime).not.toHaveBeenCalled();
    expect(h.signIn).not.toHaveBeenCalled();
  });

  it("installs missing components, then signs in", async () => {
    h.set(status({ runtime: { state: "missing" } }));
    h.store.installRuntime.mockImplementation(async () => {
      h.set(status({ runtime: { state: "installing" } }));
      setTimeout(() => h.set(status({ runtime: { state: "installed" } })), 0);
      return { ok: true, data: { started: true } };
    });
    expect(await linkXboxPc()).toBe(true);
    expect(h.store.installRuntime).toHaveBeenCalledTimes(1);
    expect(h.signIn).toHaveBeenCalledTimes(1);
  });

  it("stops with a toast when the component install is refused", async () => {
    h.set(status({ runtime: { state: "missing" } }));
    h.store.installRuntime.mockResolvedValue({ ok: false, error: "source_missing:/x" });
    expect(await linkXboxPc()).toBe(false);
    expect(h.toastError).toHaveBeenCalledWith("toasts.xboxPcRuntimeFailed", "source_missing:/x");
    expect(h.signIn).not.toHaveBeenCalled();
  });

  it("updates outdated components in the background and carries on", async () => {
    h.set(status({ runtime: { state: "outdated" }, account: { state: "signed_in" } }));
    h.store.installRuntime.mockResolvedValue({ ok: true, data: { started: true } });
    expect(await linkXboxPc({ signIn: false })).toBe(true);
    expect(h.store.installRuntime).toHaveBeenCalledTimes(1);
  });

  it("does not sign a linked account in again", async () => {
    h.set(status({ account: { state: "unverified" } }));
    expect(await linkXboxPc()).toBe(true);
    expect(h.signIn).not.toHaveBeenCalled();
  });

  it("installs the components before Microsoft is connected", async () => {
    h.set(status({ xcloud_signed_in: false, runtime: { state: "missing" } }));
    h.store.installRuntime.mockImplementation(async () => {
      h.set(status({ xcloud_signed_in: false, runtime: { state: "installed" } }));
      return { ok: true, data: { started: true } };
    });
    expect(await linkXboxPc()).toBe(false);
    expect(h.store.installRuntime).toHaveBeenCalledTimes(1);
    expect(h.signIn).not.toHaveBeenCalled();
  });

  it("never opens a sign-in at boot", async () => {
    h.set(status());
    expect(await linkXboxPc({ signIn: false })).toBe(false);
    expect(h.signIn).not.toHaveBeenCalled();
  });

  it("shares one run between concurrent callers", async () => {
    h.set(status());
    const [a, b] = await Promise.all([linkXboxPc(), linkXboxPc()]);
    expect(a && b).toBe(true);
    expect(h.signIn).toHaveBeenCalledTimes(1);
  });
});
