/**
 * Play on an Xbox PC game with missing files queues a repair. A refusal
 * must say why, never fail silently (rule 8).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  call: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("@decky/api", () => ({
  call: api.call,
  toaster: { toast: api.toast },
}));
vi.mock("i18next", () => ({
  default: { t: (k: string) => k },
}));
// Unclamped, so the assertions read whole keys.
vi.mock("../show-toast", () => ({
  showToast: (options: unknown) => api.toast(options),
}));
vi.mock("../../api/event-bus-client", () => ({
  EventBusClient: { subscribe: () => () => {} },
}));

import { repairXboxPcGame } from "./xbox-pc-repair";

beforeEach(() => {
  api.call.mockReset();
  api.toast.mockReset();
});

describe("repairXboxPcGame", () => {
  it("queues the repair for the game and stays quiet", async () => {
    api.call.mockResolvedValue({ success: true, error: null });
    expect(await repairXboxPcGame("9NMPDQ2NRX34")).toBe(true);
    expect(api.call).toHaveBeenCalledWith("repair_xbox_pc_game", "9NMPDQ2NRX34");
    expect(api.toast).not.toHaveBeenCalled();
  });

  it("toasts why a repair could not be queued", async () => {
    api.call.mockResolvedValue({
      success: false,
      error: "xbox_repair_no_record",
    });
    expect(await repairXboxPcGame("9NMPDQ2NRX34")).toBe(false);
    expect(api.toast.mock.calls[0][0]).toMatchObject({
      title: "toasts.header.gameRepair",
      body: "toasts.downloadError.xboxPcRepairNoRecord",
    });
  });
});
