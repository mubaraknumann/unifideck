/**
 * The three Luna panel states, from the Play-control side (Req 10.1-10.4).
 *
 * A Luna title is a connection-gated browser stream: `BrowserGameButtons`
 * renders a *different* control depending on the live Luna `authStatus` the
 * section hands it. This pins the one branch per state so a future refactor
 * of the gating cannot silently turn a sign-in wall back into a Play button
 * (or the reverse):
 *
 *  - connected      → an enabled "Play on Cloud" control            (10.1)
 *  - not connected  → a disabled Play + a sign-in prompt            (10.2, 10.3)
 *  - session expired→ a re-authentication prompt (distinct copy)    (10.4)
 *
 * Like the other Play-section tests, the component is called as a plain
 * function — React here is the `test-support` stub, so JSX yields a
 * descriptor object and nothing renders. `PlayMeta`'s layout primitives
 * (`PlayShell`/`MetaInline`/`IconGroup`) are stubbed to pass their children
 * through: they lean on `useEffect`/`useRef`, which the stub does not carry,
 * and they are not what these cases assert on. The translation mock returns
 * the key verbatim so assertions read against stable i18n keys, not prose.
 */
import { describe, it, expect, vi } from "vitest";

// --- Hook + UI boundaries, mocked so the component runs as a pure function.

/** Live Luna connection status fed to the gated Play control. */
const storeAuth = vi.hoisted(() => ({
  busy: false,
  connect: vi.fn(),
  disconnect: vi.fn(),
}));
/** Whether Steam reports the shortcut as running (Resume/Stop vs Play). */
const appRunning = vi.hoisted(() => ({ value: false }));
const toast = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
  info: vi.fn(),
  show: vi.fn(),
}));

vi.mock("@decky/ui", () => ({ DialogButton: "DialogButton" }));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
// Every fa icon used by the component resolves to its own string tag so a
// branch can be identified by which icon it rendered.
vi.mock("react-icons/fa", () => ({
  FaCloud: "FaCloud",
  FaExclamationTriangle: "FaExclamationTriangle",
  FaGlobe: "FaGlobe",
  FaPlay: "FaPlay",
  FaSignInAlt: "FaSignInAlt",
  FaTimes: "FaTimes",
}));
vi.mock("../shared", () => ({
  SteamControllerIcon: "SteamControllerIcon",
  SteamGearIcon: "SteamGearIcon",
}));
// Layout primitives pass children straight through; the class/style helpers
// return trivial values. Keeps the descriptor tree walkable without pulling
// in useEffect/useRef (absent from the React stub).
vi.mock("./PlayMeta", () => ({
  PlayShell: ({ children }: { children: unknown }) => ({
    type: "PlayShell",
    props: { children },
  }),
  MetaInline: () => ({ type: "MetaInline", props: {} }),
  IconGroup: ({ children }: { children: unknown }) => ({
    type: "IconGroup",
    props: { children },
  }),
  actionBtnStyle: {},
  iconBtnStyle: {},
  actionBtnClass: (marker: string) => marker,
  iconBtnClass: (marker = "unifideck-icon-btn") => marker,
  controllerBtnClass: () => "unifideck-icon-btn",
}));

vi.mock("../../hooks/useAppRunning", () => ({
  useAppRunning: () => appRunning.value,
}));
vi.mock("../../hooks/useGameActions", () => ({
  useGameActions: () => ({
    launch: vi.fn(),
    terminate: vi.fn(),
  }),
}));
vi.mock("../../hooks/useStoreAuth", () => ({
  useStoreAuth: () => storeAuth,
}));
vi.mock("../../hooks/useToast", () => ({ useToast: () => toast }));
vi.mock("../../lib/steam-bridge", () => ({ SteamBridge: class {} }));
vi.mock("../../hooks/usePlayActions", () => ({
  usePlayActions: () => ({ playBrowser: vi.fn().mockResolvedValue(undefined) }),
}));
vi.mock("./steamAppDialogs", () => ({
  openControllerConfig: vi.fn(),
  openGearMenu: vi.fn(),
}));
vi.mock("./PcUnsupportedButtons", () => ({ PcReasonLine: "PcReasonLine" }));

import { BrowserGameButtons } from "./BrowserGameButtons";
import type { StoreStatus } from "../../types/api";

const APP_ID = 3_012_345_678;
const LUNA_URL = "https://luna.amazon.de/game/amzn1.adg.product.2b77fdb6";

type Descriptor = { type?: unknown; props?: Record<string, unknown> };

/** Collect every node in the descriptor tree (depth-first). */
function flatten(node: unknown, out: Descriptor[] = []): Descriptor[] {
  if (!node || typeof node !== "object") return out;
  const d = node as Descriptor;
  if ("type" in d || "props" in d) out.push(d);
  const children = d.props?.children;
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) flatten(child, out);
  return out;
}

/** The node whose `type` matches (string tags like "DialogButton"/"FaCloud"). */
function nodesOfType(root: unknown, type: string): Descriptor[] {
  return flatten(root).filter((n) => n.type === type);
}

/** True if any node in the tree rendered the given i18n key as text. */
function hasText(root: unknown, key: string): boolean {
  return flatten(root).some((n) => {
    const children = n.props?.children;
    const list = Array.isArray(children) ? children : [children];
    return list.includes(key);
  });
}

function renderLuna(authStatus: StoreStatus | undefined, url = LUNA_URL) {
  return BrowserGameButtons({
    appId: APP_ID,
    variant: "stream",
    url,
    gatedStore: "luna",
    authStatus,
  }) as unknown;
}

describe("BrowserGameButtons — Luna panel: connected (Req 10.1)", () => {
  it("renders an enabled Play on Cloud control", () => {
    appRunning.value = false;
    const tree = renderLuna("connected");

    const buttons = nodesOfType(tree, "DialogButton");
    const play = buttons.find((b) => hasText(b, "play.playOnCloud"));
    expect(play).toBeDefined();
    // Not disabled, and wired to the launch handler (not the sign-in one).
    expect(play!.props?.disabled).toBeFalsy();
    expect(typeof play!.props?.onClick).toBe("function");

    // The cloud icon marks the stream variant; no sign-in prompt is shown.
    expect(nodesOfType(tree, "FaCloud")).toHaveLength(1);
    expect(nodesOfType(tree, "FaSignInAlt")).toHaveLength(0);
    expect(hasText(tree, "play.luna.signInHint")).toBe(false);
    expect(hasText(tree, "play.luna.sessionExpired")).toBe(false);

    expect(tree).toMatchSnapshot();
  });
});

describe("BrowserGameButtons — Luna panel: not connected (Req 10.2, 10.3)", () => {
  it("disables Play and shows a sign-in prompt", () => {
    appRunning.value = false;
    storeAuth.busy = false;
    const tree = renderLuna("disconnected");

    // The sign-in action replaces Play; its label is the first sign-in copy,
    // not the re-auth one.
    expect(nodesOfType(tree, "FaSignInAlt")).toHaveLength(1);
    expect(hasText(tree, "play.luna.signIn")).toBe(true);
    expect(hasText(tree, "play.luna.reauthenticate")).toBe(false);

    // Req 10.3: the sign-in hint prompt is present (10.2 the disabled Play:
    // there is no enabled Play on Cloud control in this state).
    expect(hasText(tree, "play.luna.signInHint")).toBe(true);
    expect(hasText(tree, "play.playOnCloud")).toBe(false);
    expect(nodesOfType(tree, "FaCloud")).toHaveLength(0);

    expect(tree).toMatchSnapshot();
  });

  it("disables the sign-in button while an auth call is in flight", () => {
    appRunning.value = false;
    storeAuth.busy = true;
    const tree = renderLuna("disconnected");
    const signIn = nodesOfType(tree, "DialogButton").find((b) => hasText(b, "play.luna.signIn"));
    expect(signIn?.props?.disabled).toBe(true);
    storeAuth.busy = false;
  });

  it("treats a missing status (not yet checked) as not connected", () => {
    appRunning.value = false;
    const tree = renderLuna(undefined);
    expect(hasText(tree, "play.luna.signIn")).toBe(true);
    expect(hasText(tree, "play.playOnCloud")).toBe(false);
  });
});

describe("BrowserGameButtons — Luna panel: session expired (Req 10.4)", () => {
  it("renders a re-authentication prompt distinct from first sign-in", () => {
    appRunning.value = false;
    storeAuth.busy = false;
    const tree = renderLuna("expired");

    // Distinct copy: reconnect / session-expired, not the first-time sign-in.
    expect(hasText(tree, "play.luna.reauthenticate")).toBe(true);
    expect(hasText(tree, "play.luna.sessionExpired")).toBe(true);
    expect(hasText(tree, "play.luna.signIn")).toBe(false);
    expect(hasText(tree, "play.luna.signInHint")).toBe(false);

    // The warning icon flags the expiry; Play on Cloud is not offered.
    expect(nodesOfType(tree, "FaExclamationTriangle")).toHaveLength(1);
    expect(hasText(tree, "play.playOnCloud")).toBe(false);

    expect(tree).toMatchSnapshot();
  });
});
