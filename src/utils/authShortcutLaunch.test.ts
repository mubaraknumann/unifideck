/**
 * `nameAndPrefix`: each shortcut action gets its own display name and
 * store_game_id prefix, so a log line or an id scan can tell an Xbox PC
 * sign-in from the xCloud one and from a shop window.
 */
import { describe, expect, it, vi } from "vitest";

vi.mock("@decky/api", () => ({ call: vi.fn() }));
vi.mock("../lib/steam-bridge", () => ({}));
vi.mock("./controllerConfig", () => ({ applyWebBrowserLayout: vi.fn() }));

import { _MICROSOFT_AUTH_CONFIG, nameAndPrefix } from "./authShortcutLaunch";

describe("nameAndPrefix", () => {
  const ms = _MICROSOFT_AUTH_CONFIG;

  it("uses the sign-in pair for auth", () => {
    expect(nameAndPrefix(ms, "auth")).toEqual({
      appName: "Microsoft Sign-In",
      idPrefix: "microsoft:ms-auth-temp",
    });
  });

  it("uses the shop pair for storefront", () => {
    expect(nameAndPrefix(ms, "storefront")).toEqual({
      appName: "Xbox Store",
      idPrefix: "microsoft:ms-store",
    });
  });

  it("gives the Xbox PC sign-in its own pair", () => {
    expect(nameAndPrefix(ms, "xbox-pc-auth")).toEqual({
      appName: "Xbox PC Sign-In",
      idPrefix: "microsoft:xbox-pc-auth-temp",
    });
  });

  it("falls back to the sign-in pair for a store without one", () => {
    const { xboxPcAppName: _n, xboxPcStoreIdPrefix: _p, ...rest } = ms;
    expect(nameAndPrefix(rest, "xbox-pc-auth")).toEqual(nameAndPrefix(ms, "auth"));
  });
});
