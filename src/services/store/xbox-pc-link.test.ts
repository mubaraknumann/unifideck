/**
 * linkXboxPc: one Microsoft sign-in brings Xbox PC up by itself.
 *
 * Pinned: nothing happens with the toggle off or Microsoft disconnected;
 * missing components install first, then the sign-in runs; a linked account
 * is not signed in again; `auto` (boot, manual sync) only opens the window for a
 * signed-out account with no game running; the sign-in does not wait for the
 * runtime; concurrent callers share one run.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

type Status = {
  enabled: boolean;
  xcloud_signed_in: boolean;
  runtime: { state: string; backend_ready?: boolean };
  account: { state: string; reset?: boolean };
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
    router: { MainRunningApp: undefined as object | undefined },
  };
});

vi.mock("@decky/ui", () => ({ Router: h.router }));
vi.mock("../../stores/xbox-pc-store", () => ({ xboxPcStore: h.store }));
vi.mock("../auth/xbox-pc-sign-in", () => ({ signInXboxPc: h.signIn }));
vi.mock("./xbox-pc-actions", () => ({ toastXboxPcError: h.toastError }));

import { linkXboxPc, waitForRuntime, watchXboxPcReset } from "./xbox-pc-link";

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
  h.store.refetch.mockImplementation(async () => {});
  h.store.refresh.mockImplementation(async () => {});
  h.router.MainRunningApp = undefined;
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
    expect(h.toastError).toHaveBeenCalledWith("component install", "source_missing:/x");
    expect(h.signIn).not.toHaveBeenCalled();
  });

  it("updates outdated components in the background and carries on", async () => {
    h.set(status({ runtime: { state: "outdated" }, account: { state: "signed_in" } }));
    h.store.installRuntime.mockResolvedValue({ ok: true, data: { started: true } });
    expect(await linkXboxPc({ auto: true })).toBe(true);
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

  it("auto opens the sign-in for a signed-out account", async () => {
    h.set(status());
    expect(await linkXboxPc({ auto: true })).toBe(true);
    expect(h.signIn).toHaveBeenCalledTimes(1);
  });

  it("auto leaves a running game alone", async () => {
    h.router.MainRunningApp = {};
    h.set(status());
    expect(await linkXboxPc({ auto: true })).toBe(false);
    expect(h.signIn).not.toHaveBeenCalled();
  });

  it.each(["error", "mismatch", "signing_in"])(
    "auto does not reopen for account state %s",
    async (state) => {
      h.set(status({ account: { state } }));
      expect(await linkXboxPc({ auto: true })).toBe(false);
      expect(h.signIn).not.toHaveBeenCalled();
    },
  );

  it("signs in while the runtime is still installing", async () => {
    h.set(status({ runtime: { state: "installing", backend_ready: true } }));
    expect(await linkXboxPc()).toBe(true);
    expect(h.signIn).toHaveBeenCalledTimes(1);
    expect(h.store.installRuntime).not.toHaveBeenCalled();
  });

  it("waits for the backend, then signs in without the runtime", async () => {
    h.set(status({ runtime: { state: "installing" } }));
    const done = linkXboxPc();
    await vi.waitFor(() => expect(h.store.refetch).toHaveBeenCalled());
    expect(h.signIn).not.toHaveBeenCalled();
    h.set(status({ runtime: { state: "installing", backend_ready: true } }));
    expect(await done).toBe(true);
    expect(h.signIn).toHaveBeenCalledTimes(1);
  });

  it("does not give up when the status still says missing after the install starts", async () => {
    h.set(status({ runtime: { state: "missing" } }));
    h.store.installRuntime.mockResolvedValue({ ok: true, data: { started: true } });
    h.store.refetch
      .mockImplementationOnce(async () => {})
      .mockImplementationOnce(async () => {
        h.set(status({ runtime: { state: "installing" } }));
        setTimeout(
          () => h.set(status({ runtime: { state: "installing", backend_ready: true } })),
          0,
        );
      });
    expect(await linkXboxPc()).toBe(true);
    expect(h.signIn).toHaveBeenCalledTimes(1);
  });

  it("keeps waiting after install_in_progress", async () => {
    h.set(status({ runtime: { state: "missing" } }));
    h.store.installRuntime.mockResolvedValue({ ok: false, error: "install_in_progress" });
    h.store.refetch
      .mockImplementationOnce(async () => {})
      .mockImplementationOnce(async () => {
        h.set(status({ runtime: { state: "installed" } }));
      });
    expect(await linkXboxPc()).toBe(true);
    expect(h.toastError).not.toHaveBeenCalled();
  });

  it("asks again when the first answer says Microsoft is not connected", async () => {
    h.set(status({ xcloud_signed_in: false }));
    h.store.refresh.mockImplementationOnce(async () => h.set(status()));
    expect(await linkXboxPc()).toBe(true);
    expect(h.signIn).toHaveBeenCalledTimes(1);
  });

  it("a caller that asked outright is not lost by joining an auto run", async () => {
    h.router.MainRunningApp = {};
    h.set(status());
    const auto = linkXboxPc({ auto: true });
    const asked = linkXboxPc();
    expect(await auto).toBe(false);
    expect(await asked).toBe(true);
    expect(h.signIn).toHaveBeenCalledTimes(1);
  });

  it("waitForRuntime waits past the backend for the whole install", async () => {
    h.set(status({ runtime: { state: "installing", backend_ready: true } }));
    let finished = false;
    const wait = waitForRuntime().then((r) => {
      finished = true;
      return r;
    });
    await Promise.resolve();
    expect(finished).toBe(false);
    h.set(status({ runtime: { state: "installed", backend_ready: true } }));
    expect(await wait).toBe(true);
  });

  it("opens the sign-in once when the backend replaces a plain sign-in", async () => {
    h.set(status({ account: { state: "signed_in" } }));
    const stop = watchXboxPcReset();
    const reset = status({ account: { state: "signed_out", reset: true } });
    h.set(reset);
    await vi.waitFor(() => expect(h.signIn).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 0));
    h.set({ ...reset });
    await new Promise((r) => setTimeout(r, 0));
    expect(h.signIn).toHaveBeenCalledTimes(1);
    stop();
  });

  it("shares one run between concurrent callers", async () => {
    h.set(status());
    const [a, b] = await Promise.all([linkXboxPc(), linkXboxPc()]);
    expect(a && b).toBe(true);
    expect(h.signIn).toHaveBeenCalledTimes(1);
  });
});
