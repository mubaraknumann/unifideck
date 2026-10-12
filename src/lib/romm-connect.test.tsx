/**
 * Test RomM connection retries and cancellation through modal callbacks.
 *
 * The modal is stubbed because the Vitest JSX runtime renders nothing.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

import { envelope } from "../test-support/rpc-envelope";

const mockCall = vi.hoisted(() => vi.fn());
vi.mock("@decky/api", () => ({
  call: (...args: unknown[]) => mockCall(...args),
}));

interface ModalProps {
  onBegin: (v: Record<string, unknown>) => Promise<unknown>;
  onPoll: () => Promise<unknown>;
  onComplete: (excluded: string[]) => Promise<void>;
  onCancel: () => void;
}
const modal = vi.hoisted(() => ({ props: null as unknown, close: vi.fn() }));
vi.mock("@decky/ui", () => ({
  showModal: (el: { props: unknown }) => {
    modal.props = el.props;
    return { Close: modal.close };
  },
}));

vi.mock("../components/modals/RomMConnectModal", () => ({
  RomMConnectModal: () => null,
}));

vi.mock("../api/rpc-routes", () => ({
  rpcRoutes: {
    connectRomm: "connect_romm",
    pollRommPairing: "poll_romm_pairing",
    completeRommConnection: "complete_romm_connection",
    cancelRommConnection: "cancel_romm_connection",
  },
}));

import { connectRomM } from "./romm-connect";

const VARIANTS = [
  { platform_slug: "linux", fs_slug: "linux", display_name: "Linux" },
  { platform_slug: "win", fs_slug: "windows", display_name: "Windows" },
];
const FORM = {
  serverUrl: "https://romm.example",
  token: "",
  verifySsl: true,
  downloadDir: "",
};

const props = () => modal.props as ModalProps;
const callsTo = (route: string) => mockCall.mock.calls.filter((c) => c[0] === route);

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

beforeEach(() => {
  mockCall.mockReset();
  modal.close.mockReset();
  modal.props = null;
});

describe("connectRomM", () => {
  it("returns pairing metadata, polls to approval, and commits only on confirmation", async () => {
    const pairing = {
      user_code: "ABCD1234",
      verification_url: "https://romm.example/pair/device?user_code=ABCD1234",
      expires_in: 600,
      interval: 5,
    };
    mockCall.mockResolvedValueOnce(envelope({ success: true, action: "pair", metadata: pairing }));
    const flow = connectRomM({ setBusy: vi.fn() });
    expect(await props().onBegin(FORM)).toEqual(pairing);
    mockCall.mockResolvedValueOnce(envelope({ success: true, action: "pending" }));
    expect(await props().onPoll()).toBeNull();
    mockCall.mockResolvedValueOnce(
      envelope({ success: true, action: "select_platforms", metadata: { variants: VARIANTS } }),
    );
    expect(await props().onPoll()).toEqual(VARIANTS);
    expect(callsTo("poll_romm_pairing")[0][1]).toBe(callsTo("connect_romm")[0][1]);
    expect(callsTo("complete_romm_connection")).toHaveLength(0);
    mockCall.mockResolvedValueOnce(envelope({ success: true }));
    await props().onComplete([]);
    expect(await flow).toMatchObject({ success: true });
  });

  it("uses a fresh attempt after a denied pairing", async () => {
    mockCall.mockResolvedValueOnce(
      envelope({
        success: true,
        action: "pair",
        metadata: {
          user_code: "1234",
          verification_url: "https://romm.example/pair",
          expires_in: 600,
          interval: 5,
        },
      }),
    );
    void connectRomM({ setBusy: vi.fn() });
    await props().onBegin(FORM);
    mockCall.mockResolvedValueOnce(envelope({ success: false, error: "denied" }));
    mockCall.mockResolvedValueOnce(envelope({ success: true }));
    await expect(props().onPoll()).rejects.toThrow("denied");
    mockCall.mockResolvedValueOnce(envelope({ success: true, metadata: { variants: VARIANTS } }));
    await props().onBegin(FORM);
    expect(callsTo("connect_romm")[0][1]).not.toBe(callsTo("connect_romm")[1][1]);
    props().onCancel();
  });
  it("settles with the completed result and never before confirmation", async () => {
    mockCall.mockImplementation((route: string) =>
      Promise.resolve(
        route === "connect_romm"
          ? envelope({ success: true, metadata: { variants: VARIANTS } })
          : envelope({ success: true }),
      ),
    );
    let settled = false;
    const flow = connectRomM({ setBusy: vi.fn() }).then((r) => {
      settled = true;
      return r;
    });

    expect(await props().onBegin(FORM)).toEqual(VARIANTS);
    await Promise.resolve();
    expect(settled).toBe(false);
    expect(callsTo("complete_romm_connection")).toHaveLength(0);

    await props().onComplete(["windows"]);
    expect(await flow).toMatchObject({ success: true, store: "romm" });
    const [, attemptId, excluded] = callsTo("complete_romm_connection")[0];
    expect(excluded).toEqual(["windows"]);
    expect(callsTo("connect_romm")[0][1]).toBe(attemptId);
  });

  it("rejects inline on failure and keeps the flow open for a retry", async () => {
    mockCall.mockResolvedValueOnce(envelope({ success: false, error: "bad credentials" }));
    let settled = false;
    void connectRomM({ setBusy: vi.fn() }).then(() => (settled = true));

    await expect(props().onBegin(FORM)).rejects.toThrow("bad credentials");
    await Promise.resolve();
    expect(settled).toBe(false);
    expect(modal.close).not.toHaveBeenCalled();
  });

  it("settles cancellation once and ignores a late begin result", async () => {
    const gate = deferred<unknown>();
    mockCall.mockImplementation((route: string) =>
      route === "connect_romm" ? gate.promise : Promise.resolve(envelope({ success: true })),
    );
    const flow = connectRomM({ setBusy: vi.fn() });
    const begin = props().onBegin(FORM);

    props().onCancel();
    props().onCancel();
    gate.resolve(envelope({ success: true, metadata: { variants: VARIANTS } }));

    expect(await flow).toBeNull();
    await expect(begin).rejects.toThrow();
    const cancels = callsTo("cancel_romm_connection");
    expect(cancels).toHaveLength(1);
    expect(cancels[0][1]).toBe(callsTo("connect_romm")[0][1]);
    expect(callsTo("complete_romm_connection")).toHaveLength(0);
  });

  it("surfaces failed cancellation and rejects late completion", async () => {
    const completing = deferred<unknown>();
    const cancelling = deferred<unknown>();
    mockCall.mockImplementation((route: string) => {
      if (route === "connect_romm")
        return Promise.resolve(envelope({ success: true, metadata: { variants: VARIANTS } }));
      if (route === "complete_romm_connection") return completing.promise;
      if (route === "cancel_romm_connection") return cancelling.promise;
      throw new Error(`Unexpected route: ${route}`);
    });
    const flow = connectRomM({ setBusy: vi.fn() });
    await props().onBegin(FORM);
    const complete = props().onComplete([]);
    props().onCancel();
    props().onCancel();
    completing.resolve(envelope({ success: true }));
    await expect(complete).rejects.toThrow("cancelled");
    cancelling.resolve(
      envelope({
        success: false,
        error: "Could not remove saved connection",
        error_code: "config_not_removed",
      }),
    );
    expect(await flow).toMatchObject({
      success: false,
      store: "romm",
      error_code: "config_not_removed",
    });
    expect(callsTo("cancel_romm_connection")).toHaveLength(1);
  });

  it("does not call cancel when dismissed before anything was submitted", async () => {
    const flow = connectRomM({ setBusy: vi.fn() });
    props().onCancel();
    expect(await flow).toBeNull();
    expect(mockCall).not.toHaveBeenCalled();
  });

  it("cancels during completion and drops the late completion", async () => {
    const gate = deferred<unknown>();
    mockCall.mockImplementation((route: string) => {
      if (route === "connect_romm")
        return Promise.resolve(envelope({ success: true, metadata: { variants: VARIANTS } }));
      if (route === "complete_romm_connection") return gate.promise;
      return Promise.resolve(envelope({ success: true }));
    });
    const flow = connectRomM({ setBusy: vi.fn() });
    await props().onBegin(FORM);
    const completing = props().onComplete([]);

    props().onCancel();
    gate.resolve(envelope({ success: true }));

    expect(await flow).toBeNull();
    await expect(completing).rejects.toThrow();
    expect(callsTo("cancel_romm_connection")).toHaveLength(1);
  });

  it("sends the pasted token and the form values in route order", async () => {
    mockCall.mockResolvedValue(envelope({ success: true, metadata: { variants: VARIANTS } }));
    void connectRomM({ setBusy: vi.fn() });
    await props().onBegin({ ...FORM, token: "tok", verifySsl: false, downloadDir: "/dl" });
    const args = callsTo("connect_romm")[0].slice(2);
    expect(args).toEqual(["https://romm.example", "tok", false, "/dl"]);
  });

  it("releases the attempt and uses a fresh ID when no platform is eligible", async () => {
    mockCall.mockImplementation((route: string) =>
      Promise.resolve(
        route === "connect_romm"
          ? envelope({ success: true, metadata: { variants: [] } })
          : envelope({ success: true }),
      ),
    );
    void connectRomM({ setBusy: vi.fn() });

    expect(await props().onBegin(FORM)).toEqual([]);
    await props().onBegin(FORM);

    const [first, second] = callsTo("connect_romm").map((c) => c[1]);
    expect(first).not.toBe(second);
    expect(callsTo("cancel_romm_connection")[0][1]).toBe(first);
  });
});
