/**
 * The Xbox PC setup predicates and error text: when Install opens setup,
 * which refusals retry, how a sign-in ends, and what a code reads as.
 */
import { describe, expect, it } from "vitest";
import {
  isSetupReady,
  isXboxPcRow,
  isXboxPcSetupError,
  needsXboxPcSetup,
  runtimeUsable,
  signInOutcome,
} from "./xbox-pc-setup";
import {
  asPcUnsupportedReason,
  splitCode,
  xboxPcErrorKey,
  xboxPcErrorText,
} from "./xbox-pc-errors";

function status(over: Record<string, unknown> = {}) {
  return {
    available: true,
    enabled: true,
    notice_version: 1,
    notice_ack_version: 1,
    xcloud_signed_in: true,
    runtime: { state: "installed", version: "0.1.0", source_dir: "/src", degraded: [] },
    account: { state: "signed_in" },
    library: {
      installable: 0,
      not_supported: 0,
      choice: 0,
      skipped: 0,
      stale: false,
      last_read: null,
    },
    ...over,
  } as never;
}

const pcRow = { store_tags: ["xbox_pc" as const] };

describe("needsXboxPcSetup", () => {
  it("is false for a row without the tag, whatever the status", () => {
    expect(needsXboxPcSetup({ store_tags: ["xcloud"] }, status({ enabled: false }))).toBe(false);
    expect(isXboxPcRow({})).toBe(false);
  });

  it("leaves an unknown status to the backend refusal", () => {
    expect(needsXboxPcSetup(pcRow, null)).toBe(false);
  });

  it.each([
    ["feature off", { enabled: false }],
    ["Microsoft signed out", { xcloud_signed_in: false }],
    ["components missing", { runtime: { state: "missing", source_dir: "", degraded: [] } }],
    ["components installing", { runtime: { state: "installing", source_dir: "", degraded: [] } }],
    ["signed out", { account: { state: "signed_out" } }],
    ["account mismatch", { account: { state: "mismatch" } }],
    ["account error", { account: { state: "error" } }],
  ])("opens setup when %s", (_name, over) => {
    expect(needsXboxPcSetup(pcRow, status(over))).toBe(true);
  });

  it.each(["signed_in", "unverified", "unknown", "signing_in"])(
    "does not force a sign-in for an account that is %s",
    (state) => {
      expect(needsXboxPcSetup(pcRow, status({ account: { state } }))).toBe(false);
    },
  );

  it("treats a missing status as not ready", () => {
    expect(isSetupReady(null)).toBe(false);
  });

  it("does not block on components with a newer build waiting (the backend accepts them)", () => {
    const outdated = {
      runtime: { state: "outdated", version: "0.1.0", source_dir: "/src", degraded: [] },
    };
    expect(needsXboxPcSetup(pcRow, status(outdated))).toBe(false);
    expect(runtimeUsable("outdated")).toBe(true);
    expect(runtimeUsable("installing")).toBe(false);
  });
});

describe("isXboxPcSetupError", () => {
  it.each([
    ["xbox_pc_disabled", true],
    ["xbox_pc_runtime_missing", true],
    ["xbox_pc_not_signed_in: run setup", true],
    ["xbox_pc_unsupported_plan", false],
    ["xbox_pc_needs_choice", false],
    ["xbox_pc_no_package", false],
    ["itch_claim_required", false],
    [undefined, false],
  ])("%s → %s", (code, expected) => {
    expect(isXboxPcSetupError(code)).toBe(expected);
  });
});

describe("signInOutcome", () => {
  it("waits while signing in", () => {
    expect(signInOutcome(status({ account: { state: "signing_in" } }))).toBeNull();
  });

  it("counts signed_in and unverified as success", () => {
    expect(signInOutcome(status())).toEqual({ ok: true });
    expect(signInOutcome(status({ account: { state: "unverified" } }))).toEqual({ ok: true });
  });

  it("names the failure", () => {
    expect(signInOutcome(status({ account: { state: "mismatch" } }))).toEqual({
      ok: false,
      error: "account_mismatch",
    });
    expect(signInOutcome(status({ account: { state: "error", error: "signin_timeout" } }))).toEqual(
      { ok: false, error: "signin_timeout" },
    );
    expect(signInOutcome(status({ account: { state: "signed_out" } }))).toEqual({
      ok: false,
      error: "window_closed",
    });
    expect(signInOutcome(null)).toEqual({ ok: false, error: "status_unavailable" });
  });
});

describe("xboxPcErrorText", () => {
  const t = ((key: string, opts?: Record<string, unknown>) =>
    opts ? `${key}|${JSON.stringify(opts)}` : key) as never;

  it("splits the detail off the code", () => {
    expect(splitCode("source_missing:/a:b")).toEqual({ code: "source_missing", detail: "/a:b" });
    expect(splitCode("edge_busy")).toEqual({ code: "edge_busy", detail: "" });
  });

  it("names the folder a missing source was looked for in (rule 8)", () => {
    expect(xboxPcErrorText("source_missing:/home/x/out", t)).toContain('"path":"/home/x/out"');
    expect(xboxPcErrorText("source_missing", t, "/fallback")).toContain('"path":"/fallback"');
  });

  it("maps the xodus credential family by prefix", () => {
    expect(xboxPcErrorKey("xodus_credential_locked: tail")).toBe(
      "xboxPc.account.errors.credentialStore",
    );
  });

  it("never drops an unknown code", () => {
    expect(xboxPcErrorKey("brand_new_code")).toBeNull();
    expect(xboxPcErrorText("brand_new_code: tail", t)).toContain("brand_new_code");
    expect(xboxPcErrorText("", t)).toContain("xboxPc.errors.generic");
  });

  it("narrows only known unsupported reasons", () => {
    expect(asPcUnsupportedReason("ea_play")).toBe("ea_play");
    expect(asPcUnsupportedReason("gamepass")).toBeNull();
    expect(asPcUnsupportedReason(null)).toBeNull();
  });
});
