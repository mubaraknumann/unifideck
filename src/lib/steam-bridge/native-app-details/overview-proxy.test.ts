import { describe, expect, it, vi } from "vitest";
import {
  actionSpecFor,
  bindActionSpec,
  isOverviewLive,
  withDisplayStatus,
  withOurFeatures,
} from "./overview-proxy";

class FakeOverview {
  appid = 3759543982;
  display_status = 11;
  selected_per_client_data = { clientid: "0", display_status: 11, status_percentage: 100 };
  per_client_data = [this.selected_per_client_data];
  GetGameID(): string {
    return `${this.appid}-gameid`;
  }
  GetPerClientData(which: string) {
    return which === "selected" ? this.selected_per_client_data : null;
  }
}

describe("withDisplayStatus", () => {
  it("answers display_status with ours and keeps identity", () => {
    const real = new FakeOverview();
    const p = withDisplayStatus(real, 9);
    expect(p.selected_per_client_data.display_status).toBe(9);
    expect(p.per_client_data[0].display_status).toBe(9);
    expect(p.display_status).toBe(9);
    // Methods run against the proxy and see our status too.
    expect(p.GetPerClientData("selected")?.display_status).toBe(9);
    // Identity is never borrowed.
    expect(p.appid).toBe(real.appid);
    expect(p.GetGameID()).toBe("3759543982-gameid");
    // The shared overview is untouched.
    expect(real.selected_per_client_data.display_status).toBe(11);
  });

  it("hides our enrichment's 100% so no '100% Complete' shows", () => {
    const p = withDisplayStatus(new FakeOverview(), 20);
    expect(p.selected_per_client_data.status_percentage).toBeUndefined();
  });

  it("steps aside while Steam launches, runs or terminates the game", () => {
    const real = new FakeOverview();
    const p = withDisplayStatus(real, 11);
    for (const live of [1, 4, 36]) {
      real.selected_per_client_data.display_status = live;
      real.display_status = live;
      expect(p.selected_per_client_data.display_status).toBe(live);
      expect(p.display_status).toBe(live);
      expect(isOverviewLive(p)).toBe(true);
    }
    real.selected_per_client_data.display_status = 11;
    expect(p.selected_per_client_data.display_status).toBe(11);
    expect(isOverviewLive(p)).toBe(false);
  });

  it("returns the same proxy for the same overview and status", () => {
    const real = new FakeOverview();
    expect(withDisplayStatus(real, 9)).toBe(withDisplayStatus(real, 9));
    expect(withDisplayStatus(real, 9)).not.toBe(withDisplayStatus(real, 11));
  });
});

describe("action specs", () => {
  it("are found through the proxy, and the latest one wins", () => {
    const p = withDisplayStatus(new FakeOverview(), 9);
    const first = vi.fn();
    const ref = { current: { onPrimary: first } };
    bindActionSpec(p, ref);
    actionSpecFor(p)?.onPrimary();
    expect(first).toHaveBeenCalledOnce();
    const second = vi.fn();
    ref.current = { onPrimary: second };
    actionSpecFor(p)?.onPrimary();
    expect(second).toHaveBeenCalledOnce();
  });

  it("are absent for a real overview", () => {
    expect(actionSpecFor(new FakeOverview())).toBeNull();
    expect(actionSpecFor(null)).toBeNull();
  });
});

describe("withOurFeatures", () => {
  const steamGame = (cats: number[]) => ({
    appid: 2908146534,
    BHasStoreCategory(c: number) {
      return cats.includes(c);
    },
  });

  it("answers Achievements and Cloud Saves from our facts", () => {
    const ov = steamGame([2, 22, 23, 28]);
    const off = withOurFeatures(ov, { achievements: false, cloudSaves: false });
    expect(off.BHasStoreCategory(22)).toBe(false);
    expect(off.BHasStoreCategory(23)).toBe(false);
    const on = withOurFeatures(steamGame([2]), { achievements: true, cloudSaves: true });
    expect(on.BHasStoreCategory(22)).toBe(true);
    expect(on.BHasStoreCategory(23)).toBe(true);
  });

  it("never claims a Steam-only service for another store's copy", () => {
    const ov = withOurFeatures(steamGame([29, 41, 42, 43, 44, 62]), {
      achievements: true,
      cloudSaves: true,
    });
    for (const c of [29, 41, 42, 43, 44, 62]) expect(ov.BHasStoreCategory(c)).toBe(false);
  });

  it("passes the game's own facts through", () => {
    const ov = withOurFeatures(steamGame([2, 28]), { achievements: false, cloudSaves: false });
    expect(ov.BHasStoreCategory(2)).toBe(true);
    expect(ov.BHasStoreCategory(28)).toBe(true);
    expect(ov.BHasStoreCategory(1)).toBe(false);
    expect(ov.appid).toBe(2908146534);
  });
});
