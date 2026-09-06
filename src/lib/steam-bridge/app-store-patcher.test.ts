// @vitest-environment jsdom
/**
 * The store patch borrows CONTENT, never IDENTITY.
 *
 * Field case (2026-08-25 bundle): Ys I and Trails in the Sky ran — audibly,
 * with a live gamescope swapchain — behind Steam's loading screen, with only
 * *Abort* available. Steam's logs show the launch tracked under the wrong
 * app:
 *
 *   Adding process 3011 for gameID 223810
 *   AppID 223810 adding PID 3011 ... reaper SteamLaunch AppId=3969905431
 *
 * `GetAppOverviewByAppID` returned `origGetOverview(realId)` — the whole
 * `AppOverview` of the matched Steam app (223810 = Ys I&II Chronicles+,
 * which the tester also owns), falling back to a synthesised overview whose
 * `appid` and `GameID()` were ALSO the real Steam app's. Either way Steam
 * resolved our shortcut to a different app, so the loading screen waited on
 * a window that never appeared.
 *
 * These pin the contract: the getters answer with our own identity, while the
 * borrowed store metadata still reaches the UI.
 */
import { describe, it, expect, afterEach, vi } from "vitest";

const SHORTCUT = 3969905431;
const REAL_STEAM_APPID = 223810;

const MAPPINGS = { success: true, mappings: { [String(SHORTCUT)]: REAL_STEAM_APPID } };
const METADATA = {
  success: true,
  metadata: {
    [String(REAL_STEAM_APPID)]: {
      name: "Ys I&II Chronicles+",
      short_description: "A borrowed store description.",
      developers: ["Nihon Falcom"],
      publishers: ["XSEED Games"],
      release_date: { date: "Feb 14, 2013" },
    },
  },
};

vi.mock("@decky/api", () => ({
  call: vi.fn(async (route: string) =>
    route === "get_real_steam_appid_mappings" ? MAPPINGS : METADATA,
  ),
}));

interface Overview extends Record<string, unknown> {
  appid: number;
  display_name: string;
}

/** Steam's own overview for our shortcut — what must always come back. */
function shortcutOverview(): Overview {
  return { appid: SHORTCUT, display_name: "Ys I", gameid: "ignored" };
}

/** Steam's overview for the owned copy — must never be handed out for the
 *  shortcut's id. */
function ownedOverview(): Overview {
  return { appid: REAL_STEAM_APPID, display_name: "Ys I&II Chronicles+" };
}

function installStores(owned: boolean) {
  const overviews = new Map<number, Overview>([[SHORTCUT, shortcutOverview()]]);
  if (owned) overviews.set(REAL_STEAM_APPID, ownedOverview());
  const details = new Map<number, Record<string, unknown>>([
    [SHORTCUT, { unAppID: SHORTCUT, strDisplayName: "Ys I" }],
  ]);
  if (owned) {
    details.set(REAL_STEAM_APPID, {
      unAppID: REAL_STEAM_APPID,
      strDisplayName: "Ys I&II Chronicles+",
      strDescription: "Steam's own copy.",
    });
  }
  const win = window as unknown as Record<string, unknown>;
  win.appStore = {
    m_mapApps: overviews,
    GetAppOverviewByAppID: (id: number) => overviews.get(id) ?? null,
  };
  win.appDetailsStore = {
    GetAppDetails: (id: number) => details.get(id) ?? null,
  };
  return win;
}

afterEach(() => {
  const win = window as unknown as Record<string, unknown>;
  delete win.appStore;
  delete win.appDetailsStore;
  vi.resetModules();
});

async function applyPatch() {
  const mod = await import("./app-store-patcher");
  return mod.applyAppStorePatch();
}

describe("GetAppOverviewByAppID", () => {
  it("returns the shortcut's own overview when the game is ALSO owned on Steam", async () => {
    const win = installStores(true);
    const handle = await applyPatch();
    const store = win.appStore as { GetAppOverviewByAppID: (i: number) => Overview };

    const ov = store.GetAppOverviewByAppID(SHORTCUT);

    expect(ov.appid).toBe(SHORTCUT);
    expect(ov.appid).not.toBe(REAL_STEAM_APPID);
    handle.remove();
  });

  it("returns the shortcut's own overview when the game is NOT owned on Steam", async () => {
    // The old fallback synthesised an overview with `appid: realId` and
    // `GameID: () => String(realId)` — the same identity leak, for the
    // titles the user does not own.
    const win = installStores(false);
    const handle = await applyPatch();
    const store = win.appStore as { GetAppOverviewByAppID: (i: number) => Overview };

    const ov = store.GetAppOverviewByAppID(SHORTCUT);

    expect(ov.appid).toBe(SHORTCUT);
    handle.remove();
  });

  it("leaves an unmapped app completely alone", async () => {
    const win = installStores(true);
    const handle = await applyPatch();
    const store = win.appStore as {
      GetAppOverviewByAppID: (i: number) => Overview | null;
    };

    expect(store.GetAppOverviewByAppID(REAL_STEAM_APPID)?.appid).toBe(REAL_STEAM_APPID);
    handle.remove();
  });

  it("still injects the borrowed store metadata onto our own overview", async () => {
    const win = installStores(true);
    const handle = await applyPatch();
    const store = win.appStore as { GetAppOverviewByAppID: (i: number) => Overview };

    store.GetAppOverviewByAppID(SHORTCUT);
    await Promise.resolve();
    await Promise.resolve();
    const ov = store.GetAppOverviewByAppID(SHORTCUT);

    expect(ov.appid).toBe(SHORTCUT);
    expect(ov.developer).toBe("Nihon Falcom");
    expect(ov.publisher).toBe("XSEED Games");
    expect(ov.short_description).toBe("A borrowed store description.");
    handle.remove();
  });
});

describe("GetAppDetails", () => {
  it("borrows the store copy but keeps the shortcut's unAppID", async () => {
    const win = installStores(true);
    const handle = await applyPatch();
    const store = win.appDetailsStore as {
      GetAppDetails: (i: number) => Record<string, unknown>;
    };

    const details = store.GetAppDetails(SHORTCUT);

    expect(details.unAppID).toBe(SHORTCUT);
    expect(details.strDescription).toBe("Steam's own copy.");
    handle.remove();
  });

  it("falls back to the cached metadata when the app is not owned", async () => {
    const win = installStores(false);
    const handle = await applyPatch();
    const store = win.appDetailsStore as {
      GetAppDetails: (i: number) => Record<string, unknown>;
    };

    const details = store.GetAppDetails(SHORTCUT);

    expect(details.unAppID).toBe(SHORTCUT);
    expect(details.strDescription).toBe("A borrowed store description.");
    handle.remove();
  });
});
