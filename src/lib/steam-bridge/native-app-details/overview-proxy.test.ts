import { describe, expect, it, vi } from "vitest";
import {
  actionSpecFor,
  bindActionSpec,
  bindStreamMenu,
  CLOUD_CLIENT_ID,
  isOverviewLive,
  streamMenuFor,
  withDisplayStatus,
  withOurFeatures,
  withPlayTargets,
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

  it("does not read our own Cancel status as Steam being live", () => {
    // Cancel is status 1, which is also one of Steam's live statuses.
    const real = new FakeOverview();
    const p = withDisplayStatus(real, 1);
    expect(p.selected_per_client_data.display_status).toBe(1);
    expect(isOverviewLive(p)).toBe(false);
    real.selected_per_client_data.display_status = 4;
    expect(isOverviewLive(p)).toBe(true);
  });

  it("finds the real status when selected_per_client_data is a getter", () => {
    // Steam's own shape: the getter runs with the proxy as `this`, so it
    // reads our proxied per_client_data and returns an already-proxied row.
    class GetterOverview {
      selected_clientid = "0";
      per_client_data = [{ clientid: "0", display_status: 9 }];
      get selected_per_client_data() {
        return this.per_client_data.find(
          (pcd) => pcd.clientid === this.selected_clientid,
        );
      }
    }
    const real = new GetterOverview();
    const p = withDisplayStatus(real, 1);
    expect(p.selected_per_client_data?.display_status).toBe(1);
    expect(isOverviewLive(p)).toBe(false);
    real.per_client_data[0].display_status = 4;
    expect(isOverviewLive(p)).toBe(true);
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

describe("withPlayTargets", () => {
  it("offers a second, remote client so Steam draws its Play-from arrow", () => {
    const real = new FakeOverview();
    const p = withPlayTargets(withDisplayStatus(real, 9), "Xbox Cloud", false);
    expect(p.per_client_data).toHaveLength(2);
    expect(p.per_client_data[1]).toMatchObject({
      clientid: CLOUD_CLIENT_ID,
      client_name: "Xbox Cloud",
    });
    // Local stays selected: Steam keeps its Install.
    expect(p.selected_per_client_data.clientid).toBe("0");
    expect(p.selected_per_client_data.display_status).toBe(9);
    expect(p.appid).toBe(real.appid);
    expect(real.per_client_data).toHaveLength(1);
  });

  it("selects the cloud as a remote Play, which Steam shows as Stream", () => {
    const p = withPlayTargets(withDisplayStatus(new FakeOverview(), 9), "Xbox Cloud", true);
    expect(p.selected_per_client_data.clientid).toBe(CLOUD_CLIENT_ID);
    expect(p.selected_per_client_data.display_status).toBe(11);
    expect(p.display_status).toBe(11);
  });

  it("makes a cloud-only game a Stream with no arrow", () => {
    const p = withPlayTargets(new FakeOverview(), "Xbox Cloud", true, false);
    expect(p.per_client_data).toHaveLength(1);
    expect(p.selected_per_client_data.clientid).toBe(CLOUD_CLIENT_ID);
    expect(p.selected_per_client_data.display_status).toBe(11);
  });

  it("steps aside while Steam runs the game", () => {
    const real = new FakeOverview();
    real.selected_per_client_data.display_status = 4; // running
    const p = withPlayTargets(real, "Xbox Cloud", true);
    expect(p.per_client_data).toHaveLength(1);
    expect(p.selected_per_client_data.clientid).toBe("0");
  });

  it("caches one proxy per overview and choice", () => {
    const base = withDisplayStatus(new FakeOverview(), 9);
    expect(withPlayTargets(base, "Xbox Cloud", true)).toBe(
      withPlayTargets(base, "Xbox Cloud", true),
    );
    expect(withPlayTargets(base, "Xbox Cloud", false)).not.toBe(
      withPlayTargets(base, "Xbox Cloud", true),
    );
  });

  it("binds the arrow's menu to the proxy only", () => {
    const p = withPlayTargets(new FakeOverview(), "Xbox Cloud", false);
    const open = vi.fn();
    bindStreamMenu(p, { current: open });
    const anchor = {} as EventTarget;
    streamMenuFor(p)?.(anchor);
    expect(open).toHaveBeenCalledWith(anchor);
    expect(streamMenuFor(new FakeOverview())).toBeNull();
  });
});
