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
import { envelope } from "../../test-support/rpc-envelope";

const SHORTCUT = 3969905431;
const REAL_STEAM_APPID = 223810;
const LAUNCHER_EXE = '"/home/deck/homebrew/plugins/Unifideck/bin/unifideck-launcher"';

// Real wire shape: `{success, error, data: {mappings}}`. The backend has
// answered like this since 2026-05; fixtures that handed the bare
// `{success, mappings}` kept this suite green while production loaded
// `0 mappings, 0 metadata entries` on every boot.
const MAPPINGS = envelope({
  mappings: { [String(SHORTCUT)]: REAL_STEAM_APPID },
});
const METADATA = envelope({
  metadata: {
    [String(REAL_STEAM_APPID)]: {
      name: "Ys I&II Chronicles+",
      short_description: "A borrowed store description.",
      developers: ["Nihon Falcom"],
      publishers: ["XSEED Games"],
      release_date: { date: "Feb 14, 2013" },
      dlc: [223811],
      achievements: { total: 12 },
      categories: [{ id: 23 }],
    },
  },
});

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
  // What Steam really holds for a non-Steam shortcut: identity plus the
  // shortcut config `shortcut-ownership.ts` and the Properties dialog read.
  const details = new Map<number, Record<string, unknown>>([
    [
      SHORTCUT,
      {
        unAppID: SHORTCUT,
        strDisplayName: "Ys I",
        strShortcutExe: LAUNCHER_EXE,
        strShortcutLaunchOptions: "gog:1422440106",
        strShortcutStartDir: '"/home/deck"',
        bCloudAvailable: false,
        bCloudEnabledForApp: false,
        achievements: { nAchieved: 0, nTotal: 0 },
        vecDLC: [],
        eSteamInputControllerMask: 0,
      },
    ],
  ]);
  if (owned) {
    details.set(REAL_STEAM_APPID, {
      unAppID: REAL_STEAM_APPID,
      strDisplayName: "Ys I&II Chronicles+",
      strDescription: "Steam's own copy.",
      strShortcutExe: "",
      bCloudAvailable: true,
      bCloudEnabledForApp: true,
      achievements: { nAchieved: 3, nTotal: 12 },
      vecDLC: [{ appid: 223811, strName: "OST", bInstalled: true }],
      eSteamInputControllerMask: 2,
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

  it("keeps the shortcut's own config and per-app state when borrowing from the owned copy", async () => {
    const win = installStores(true);
    const handle = await applyPatch();
    const store = win.appDetailsStore as {
      GetAppDetails: (i: number) => Record<string, unknown>;
    };

    const details = store.GetAppDetails(SHORTCUT);

    // Content is borrowed …
    expect(details.strDescription).toBe("Steam's own copy.");
    // … identity and shortcut config are ours …
    expect(details.unAppID).toBe(SHORTCUT);
    expect(details.strDisplayName).toBe("Ys I");
    expect(details.strShortcutExe).toBe(LAUNCHER_EXE);
    expect(details.strShortcutLaunchOptions).toBe("gog:1422440106");
    expect(details.strShortcutStartDir).toBe('"/home/deck"');
    // … and nothing that belongs to the Steam app leaks across.
    expect(details.bCloudAvailable).toBe(false);
    expect(details.bCloudEnabledForApp).toBe(false);
    expect(details.achievements).toEqual({ nAchieved: 0, nTotal: 0 });
    expect(details.vecDLC).toEqual([]);
    expect(details.eSteamInputControllerMask).toBe(0);
    handle.remove();
  });

  it("keeps the shortcut's own config when borrowing from the cached metadata", async () => {
    const win = installStores(false);
    const handle = await applyPatch();
    const store = win.appDetailsStore as {
      GetAppDetails: (i: number) => Record<string, unknown>;
    };

    const details = store.GetAppDetails(SHORTCUT);

    expect(details.strDescription).toBe("A borrowed store description.");
    expect(details.strDeveloperName).toBe("Nihon Falcom");
    expect(details.strShortcutExe).toBe(LAUNCHER_EXE);
    expect(details.strShortcutLaunchOptions).toBe("gog:1422440106");
    expect(details.bCloudAvailable).toBe(false);
    expect(details.vecDLC).toEqual([]);
    expect(details.achievements).toEqual({ nAchieved: 0, nTotal: 0 });
    // The cached metadata carried dlc/achievements/cloud category; none of
    // it may be synthesised onto a shortcut.
    expect(details).not.toHaveProperty("__from_web_api");
    handle.remove();
  });

  it("answers null, not a made-up object, before Steam has loaded the shortcut's details", async () => {
    // A partial `{unAppID, strDisplayName, …borrowed}` object made Steam
    // render App Details early and crash in `BUserHasContentToClaim` with
    // `vecChildConfigApps is not iterable`.
    for (const owned of [true, false]) {
      const win = installStores(owned);
      const store = win.appDetailsStore as {
        GetAppDetails: (i: number) => Record<string, unknown> | null;
      };
      const steamGetDetails = store.GetAppDetails;
      store.GetAppDetails = function detailsNotYetLoaded(id) {
        return id === SHORTCUT ? null : steamGetDetails(id);
      };
      const handle = await applyPatch();

      expect(store.GetAppDetails(SHORTCUT)).toBeNull();
      handle.remove();
      vi.resetModules();
    }
  });

  it("still lets shortcut-ownership recognise a mapped shortcut as ours", async () => {
    installStores(true);
    const handle = await applyPatch();
    const { isUnifideckShortcut } = await import("./shortcut-ownership");

    expect(isUnifideckShortcut(SHORTCUT)).toBe(true);
    handle.remove();
  });
});

describe("loadFromBackend", () => {
  it("reads the {success, error, data} envelope (mappings resolve)", async () => {
    const win = installStores(false);
    const spy = vi.spyOn(console, "log").mockImplementation(() => {});
    const handle = await applyPatch();
    const store = win.appDetailsStore as {
      GetAppDetails: (i: number) => Record<string, unknown>;
    };

    // Only a resolved mapping makes GetAppDetails borrow anything.
    expect(store.GetAppDetails(SHORTCUT).strDescription).toBe("A borrowed store description.");
    expect(spy).toHaveBeenCalledWith(
      expect.stringContaining("active — 1 mappings, 1 metadata entries"),
    );
    spy.mockRestore();
    handle.remove();
  });

  it("tolerates a legacy pre-envelope reply", async () => {
    const api = await import("@decky/api");
    const call = api.call as unknown as ReturnType<typeof vi.fn>;
    call.mockImplementationOnce(async () => ({
      success: true,
      mappings: { [String(SHORTCUT)]: REAL_STEAM_APPID },
    }));
    const win = installStores(false);
    const handle = await applyPatch();
    const store = win.appDetailsStore as {
      GetAppDetails: (i: number) => Record<string, unknown>;
    };

    expect(store.GetAppDetails(SHORTCUT).strDescription).toBe("A borrowed store description.");
    handle.remove();
  });
});
