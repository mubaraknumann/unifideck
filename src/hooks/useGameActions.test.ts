/**
 * `useGameActions` resolves a refused install or uninstall as
 * `{success: false, error}`, never `null`.
 *
 * `DownloadContext` rejects with an `RpcError` whose message is the
 * backend's refusal code. Before this, the hook turned that into `null`,
 * so the itch claim retry and the Xbox PC setup retry in `useInstallFlow`
 * could never see the code, and refusals were silent (CLAUDE.md rule 8).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RpcError } from "../api/rpc-errors";

const downloads = vi.hoisted(() => ({
  installGame: vi.fn(),
  uninstallGame: vi.fn(),
  cancelDownload: vi.fn(),
  updateGame: vi.fn(),
}));
const invalidate = vi.hoisted(() => vi.fn());

vi.mock("../contexts/DownloadContext", () => ({ useDownloads: () => downloads }));
vi.mock("./useGameInfo", () => ({ invalidateGameInfo: invalidate }));
vi.mock("../lib/game-state-version", () => ({ bumpGameStateVersion: vi.fn() }));
vi.mock("../stores/update-store", () => ({ UpdateStore: { clearGame: vi.fn() } }));

import { useGameActions } from "./useGameActions";

const bridge = { runGame: vi.fn(), terminateApp: vi.fn() };
const refusal = (code: string) =>
  new RpcError("UNKNOWN", code, "install_game", { success: false, error: code });

beforeEach(() => {
  Object.values(downloads).forEach((f) => f.mockReset());
  invalidate.mockReset();
});

describe("useGameActions refusals", () => {
  it("turns a refused install into a failed result with the code", async () => {
    downloads.installGame.mockRejectedValue(refusal("xbox_pc_disabled"));
    const result = await useGameActions(bridge).install("microsoft", "9PK087LNGJC5");
    expect(result).toEqual({ success: false, error: "xbox_pc_disabled" });
  });

  it("keeps the itch claim code reachable for the retry", async () => {
    downloads.installGame.mockRejectedValue(refusal("itch_claim_required"));
    const result = await useGameActions(bridge).install("itch", "1");
    expect(result.error).toBe("itch_claim_required");
  });

  it("passes a successful install through", async () => {
    downloads.installGame.mockResolvedValue({ success: true });
    expect(await useGameActions(bridge).install("gog", "1")).toEqual({ success: true });
  });

  it("turns the real success answer, which carries no data, into a result", async () => {
    // install_game returns only {success, error}; the envelope lifts both out.
    downloads.installGame.mockResolvedValue(null);
    expect(await useGameActions(bridge).install("gog", "1")).toEqual({ success: true });
  });

  it("turns a refused uninstall into a failed result and keeps the cache", async () => {
    downloads.uninstallGame.mockRejectedValue(refusal("xbox_game_running"));
    const result = await useGameActions(bridge).uninstall(123);
    expect(result).toEqual({ success: false, error: "xbox_game_running" });
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("rethrows anything that is not an RpcError (a bug of ours)", async () => {
    downloads.installGame.mockRejectedValue(new TypeError("x is undefined"));
    await expect(useGameActions(bridge).install("gog", "1")).rejects.toThrow(TypeError);
  });
});
