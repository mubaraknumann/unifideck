/**
 * The claim gate's two predicates: which games open the claim page, and
 * which `install_game` errors mean "claim it first".
 */
import { describe, expect, it, vi } from "vitest";

vi.mock("@decky/api", () => ({ call: vi.fn() }));
vi.mock("@decky/ui", () => ({ showModal: vi.fn(), ConfirmModal: () => null }));
vi.mock("./StorefrontLauncher", () => ({
  BROWSER_MISSING: "edge_not_installed",
  openStorefrontWindow: vi.fn(),
}));

import { isClaimRequiredError, needsClaim } from "./claim-flow";

describe("needsClaim", () => {
  it("is true only for a game tagged claim_required", () => {
    expect(needsClaim({ store_tags: ["claim_required"] })).toBe(true);
    expect(needsClaim({ store_tags: ["browser"] })).toBe(false);
    expect(needsClaim({})).toBe(false);
  });
});

describe("isClaimRequiredError", () => {
  it.each([
    ["itch_claim_required: claim this game from its bundle on itch.io first", true],
    ["itch_claim_required", true],
    ["itch_no_installable_upload: no Linux or Windows build", false],
    ["claim_required_elsewhere", false],
    [undefined, false],
  ])("%s → %s", (error, expected) => {
    expect(isClaimRequiredError(error)).toBe(expected);
  });
});
