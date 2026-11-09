import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@decky/api", () => ({ call: vi.fn() }));
const dialog = vi.fn();
vi.mock("@decky/ui", () => ({
  findModuleExport: (filter: (e: unknown) => boolean) => {
    const fake = Object.assign((...args: unknown[]) => dialog(...args), {
      toString: () => 'console.log("showing launch options",E.appid)',
    });
    return filter(fake) ? fake : undefined;
  },
}));
vi.mock("../library-filters", () => ({ getUnifideckGame: vi.fn() }));

import { call } from "@decky/api";
import { getUnifideckGame } from "../library-filters";
import {
  appIdFromGameId,
  applyBattlenetLaunchPicker,
  buildOptions,
  choiceFor,
  readAskFlag,
  rememberedKey,
  steamHash,
  withAskFlag,
} from "./battlenet-launch-picker";

const APP_ID = 3143860659;
const GAME_ID = ((BigInt(APP_ID) << 32n) | 0x02000000n).toString();
const CHOICES = [
  { uid: "wow", label: "World of Warcraft" },
  { uid: "wow_classic_era", label: "World of Warcraft Classic" },
];

describe("pure helpers", () => {
  it("reads the appid out of a shortcut game id", () => {
    expect(appIdFromGameId(GAME_ID)).toBe(APP_ID);
    expect(appIdFromGameId("not-a-number")).toBeNull();
  });

  it("lists every version, then the client itself", () => {
    const rows = buildOptions(CHOICES);
    expect(rows.map((r) => r.strDescription)).toEqual([
      "World of Warcraft",
      "World of Warcraft Classic",
      "Battle.net",
    ]);
    expect(rows.map((r) => r.nIndex)).toEqual([0, 1, 2]);
  });

  it("shows the popup even with a single version installed", () => {
    expect(buildOptions([CHOICES[0]])).toHaveLength(2);
  });

  it("maps a pick to what the launcher is told", () => {
    expect(choiceFor(CHOICES, 1)).toBe("wow_classic_era");
    expect(choiceFor(CHOICES, 2)).toBe("client");
  });
});

describe("the ask switch", () => {
  it("asks unless the user set it to no", () => {
    expect(readAskFlag("battlenet:wow")).toBe(true);
    expect(readAskFlag("battlenet:wow BNET_LAUNCH_OPTIONS=yes")).toBe(true);
    expect(readAskFlag("battlenet:wow BNET_LAUNCH_OPTIONS=NO")).toBe(false);
    expect(readAskFlag(undefined)).toBe(true);
  });

  it("sets the switch in place, keeping the user's other options", () => {
    expect(withAskFlag("battlenet:wow MY=1 BNET_LAUNCH_OPTIONS=yes", false)).toBe(
      "battlenet:wow MY=1 BNET_LAUNCH_OPTIONS=no",
    );
    expect(withAskFlag("battlenet:wow", true)).toBe("battlenet:wow BNET_LAUNCH_OPTIONS=yes");
  });

  it("keys a remembered pick exactly as Steam does", () => {
    // Steam's own hash of this list, read from the live client 2026-11-01.
    const rows = [
      { nIndex: 0, strDescription: "World of Warcraft", strGameName: "", eType: 0 },
      { nIndex: 1, strDescription: "Battle.net", strGameName: "", eType: 0 },
    ];
    expect(steamHash(JSON.stringify(rows))).toBe(869538759);
    expect(rememberedKey(7, rows)).toBe("Apps\\7\\DefaultLaunchOption\\33d41bc7");
  });
});

describe("RunGame wrapper", () => {
  const original = vi.fn();
  const setOptions = vi.fn();
  let handle: { unpatch: () => void } | null = null;
  let stored: Map<string, string>;
  let launchOptions: string;

  beforeEach(() => {
    original.mockReset();
    dialog.mockReset();
    setOptions.mockReset();
    vi.mocked(call).mockReset();
    stored = new Map();
    launchOptions = "battlenet:wow BNET_LAUNCH_OPTIONS=yes";
    setOptions.mockImplementation((_id: number, opts: string) => {
      launchOptions = opts;
    });
    vi.stubGlobal("window", {
      SteamClient: {
        Apps: { RunGame: original, SetShortcutLaunchOptions: setOptions },
        Storage: {
          GetString: async (key: string) => stored.get(key) ?? null,
          DeleteKey: async (key: string) => stored.delete(key),
        },
      },
      appDetailsStore: {
        GetAppDetails: () => ({ strLaunchOptions: launchOptions }),
      },
    });
    handle = applyBattlenetLaunchPicker();
  });

  afterEach(() => {
    handle?.unpatch();
    vi.unstubAllGlobals();
  });

  const runGame = () =>
    (
      window as unknown as {
        SteamClient: { Apps: { RunGame: (...a: unknown[]) => unknown } };
      }
    ).SteamClient.Apps.RunGame(GAME_ID, "", -1, 100);

  const asBattlenet = () =>
    vi.mocked(getUnifideckGame).mockReturnValue({
      store: "battlenet",
      storeGameId: "wow",
      isInstalled: true,
    });

  const backend = (recorded: boolean, choices = CHOICES) =>
    vi
      .mocked(call)
      .mockImplementation(async (route: string) =>
        route === "get_battlenet_launch_choices"
          ? { success: true, data: { choices } }
          : { success: true, data: { recorded } },
      );

  const continueWith = async (index: number) => {
    await vi.waitFor(() => expect(dialog).toHaveBeenCalledTimes(1));
    const [props] = dialog.mock.calls[0] as [{ continue: (i: number) => void }];
    props.continue(index);
  };

  it("leaves non-Battle.net games to Steam", () => {
    vi.mocked(getUnifideckGame).mockReturnValue(null);
    runGame();
    expect(original).toHaveBeenCalledTimes(1);
    expect(dialog).not.toHaveBeenCalled();
  });

  it("records the pick, then starts the shortcut with Steam's own RunGame", async () => {
    asBattlenet();
    backend(true);
    runGame();
    await continueWith(1);
    await vi.waitFor(() => expect(original).toHaveBeenCalledTimes(1));
    expect(original).toHaveBeenCalledWith(GAME_ID, "", -1, 100);
    expect(vi.mocked(call)).toHaveBeenCalledWith(
      "set_battlenet_launch_choice",
      "wow",
      "wow_classic_era",
    );
    // The RPCs take the bare id the store's id map is keyed on.
    expect(vi.mocked(call)).toHaveBeenCalledWith("get_battlenet_launch_choices", "wow");
  });

  it("records the client choice for the Battle.net row", async () => {
    asBattlenet();
    backend(true);
    runGame();
    await continueWith(2);
    await vi.waitFor(() =>
      expect(vi.mocked(call)).toHaveBeenCalledWith("set_battlenet_launch_choice", "wow", "client"),
    );
  });

  it("does not launch when the backend refuses the pick", async () => {
    asBattlenet();
    backend(false);
    runGame();
    await continueWith(1);
    await vi.waitFor(() =>
      expect(vi.mocked(call)).toHaveBeenCalledWith(
        "set_battlenet_launch_choice",
        "wow",
        "wow_classic_era",
      ),
    );
    await new Promise((r) => setTimeout(r, 10));
    expect(original).not.toHaveBeenCalled();
  });

  it("launches as usual when the backend offers no version", async () => {
    asBattlenet();
    backend(true, []);
    runGame();
    await vi.waitFor(() => expect(original).toHaveBeenCalledTimes(1));
    expect(dialog).not.toHaveBeenCalled();
  });

  it("restores Steam's RunGame on unpatch", () => {
    handle?.unpatch();
    handle = null;
    expect(
      (window as unknown as { SteamClient: { Apps: { RunGame: unknown } } }).SteamClient.Apps
        .RunGame,
    ).toBe(original);
  });

  const rows = () => buildOptions(CHOICES);

  it("always shows the dialog while the switch is yes", async () => {
    asBattlenet();
    backend(true);
    stored.set(rememberedKey(APP_ID, rows()), "0");
    runGame();
    await vi.waitFor(() => expect(dialog).toHaveBeenCalledTimes(1));
    const [props] = dialog.mock.calls[0] as [{ alwaysShowDialog: boolean }];
    expect(props.alwaysShowDialog).toBe(true);
    // The stale pick is cleared, or it would read as "Forever" afterwards.
    expect(stored.size).toBe(0);
  });

  it("'Forever' turns the switch to no", async () => {
    asBattlenet();
    backend(true);
    runGame();
    await vi.waitFor(() => expect(dialog).toHaveBeenCalledTimes(1));
    const [props] = dialog.mock.calls[0] as [{ continue: (i: number) => void }];
    stored.set(rememberedKey(APP_ID, rows()), "1"); // what Steam writes
    props.continue(1);
    await vi.waitFor(() => expect(launchOptions).toBe("battlenet:wow BNET_LAUNCH_OPTIONS=no"));
  });

  it("'Just once' leaves the switch at yes", async () => {
    asBattlenet();
    backend(true);
    runGame();
    await continueWith(0);
    await vi.waitFor(() => expect(original).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 1700));
    expect(launchOptions).toBe("battlenet:wow BNET_LAUNCH_OPTIONS=yes");
  });

  it("with no, launches the remembered version without asking", async () => {
    asBattlenet();
    backend(true);
    launchOptions = "battlenet:wow BNET_LAUNCH_OPTIONS=no";
    stored.set(rememberedKey(APP_ID, rows()), "1");
    runGame();
    await vi.waitFor(() => expect(original).toHaveBeenCalledTimes(1));
    expect(dialog).not.toHaveBeenCalled();
    expect(vi.mocked(call)).toHaveBeenCalledWith(
      "set_battlenet_launch_choice",
      "wow",
      "wow_classic_era",
    );
  });

  it("with no, a changed set of versions goes back to asking", async () => {
    asBattlenet();
    backend(true);
    launchOptions = "battlenet:wow BNET_LAUNCH_OPTIONS=no";
    // Remembered for the old list (Retail only), not for today's.
    stored.set(rememberedKey(APP_ID, buildOptions([CHOICES[0]])), "0");
    runGame();
    await vi.waitFor(() => expect(dialog).toHaveBeenCalledTimes(1));
    expect(launchOptions).toBe("battlenet:wow BNET_LAUNCH_OPTIONS=yes");
  });
});
