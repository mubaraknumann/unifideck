/**
 * The Play-section decision, through the hook and through the pure
 * `resolvePlaySection` it delegates to.
 *
 * Audit §3.5 bullet 1. An xCloud-only title has nothing to install, and
 * the reason a user never sees an Install button for one is the ordering
 * here: an `xcloud`-tagged game resolves to its own play state *before* the
 * `not-installed` branch, so `NotInstalledButtons` (the only Install entry
 * point in the UI) never mounts.
 *
 * Xbox PC titles are the Microsoft games that really do install. They are
 * keyed by the `xbox_pc` tag, never by store name, and get their own table
 * (see `resolvePlaySection.ts`). The last suite walks every row of it,
 * including the two states that must not show Install: a status still
 * loading (skeleton) and a status that failed to load (never read as
 * "turned off").
 */
import { describe, it, expect, vi, afterEach } from "vitest";

const gameInfo = vi.hoisted(() => ({
  data: null as unknown,
  status: undefined as string | undefined,
}));
const downloads = vi.hoisted(() => ({ queue: null as unknown }));
const xbox = vi.hoisted(() => ({
  status: null as unknown,
  loading: false,
  error: null as string | null,
}));

vi.mock("./useGameInfo", () => ({ useGameInfo: () => gameInfo }));
vi.mock("../contexts/DownloadContext", () => ({
  useDownloads: () => downloads,
}));
vi.mock("./useXboxPcStatus", () => ({ useXboxPcStatus: () => xbox }));

import { usePlaySection } from "./usePlaySection";
import { resolvePlaySection } from "./resolvePlaySection";

const APP_ID = 3_012_345_678;

function game(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "9NXR0000TEST",
    store_game_id: "9NXR0000TEST",
    title: "A Cloud Game",
    store: "microsoft",
    is_installed: false,
    store_tags: ["xcloud"],
    ...over,
  };
}

/**
 * Set the mocked inputs and evaluate the hook.
 *
 * Named `useResolved` so eslint's rules-of-hooks sees a hook calling a
 * hook. The stubbed `useMemo` runs its factory synchronously, so this is
 * a plain function call — there is no React render here to violate.
 */
function useResolved(g: Record<string, unknown> | null, queue: unknown = null) {
  gameInfo.data = g;
  downloads.queue = queue;
  return usePlaySection(APP_ID);
}

describe("usePlaySection — cloud titles never reach the install path", () => {
  it("resolves an xcloud game to the browser state, not not-installed", () => {
    const state = useResolved(game());
    expect(state.kind).toBe("browser");
  });

  it("does the same when the store is not microsoft", () => {
    // The branch keys on the tag, not the store name — a second
    // subscription store would inherit the guard for free, and a
    // store-name check here would silently not.
    expect(useResolved(game({ store: "someday" })).kind).toBe("browser");
  });

  it("still offers install for a Microsoft game WITHOUT the tag", () => {
    // The hook keys on tags, never on the store name. An untagged row is
    // a generic installable row, and the backend's `install_precondition`
    // is the thing that says no for it.
    expect(useResolved(game({ store_tags: [] })).kind).toBe("not-installed");
    expect(useResolved(game({ store_tags: undefined })).kind).toBe("not-installed");
  });

  it("takes precedence over an in-flight download for the same game", () => {
    // Ordering matters: the queue check sits below the tag check, so a
    // stray queued row for a cloud title cannot put a Cancel button on a
    // game that is not downloading.
    const state = useResolved(game(), {
      current: { game_id: "9NXR0000TEST", store: "microsoft" },
      queued: [],
    });
    expect(state.kind).toBe("browser");
  });

  it("takes precedence over is_installed in either direction", () => {
    expect(useResolved(game({ is_installed: true })).kind).toBe("browser");
  });

  it("marks an xcloud title as a stream on its play URL", () => {
    const state = useResolved(game());
    expect(state).toMatchObject({
      kind: "browser",
      variant: "stream",
      url: "https://www.xbox.com/play/launch/9NXR0000TEST",
    });
  });

  it("opens an itch.io HTML5 game as a web page", () => {
    const state = useResolved(
      game({
        store: "itch",
        store_tags: ["browser"],
        browser_url: "https://poncle.itch.io/vampire-survivors",
      }),
    );
    expect(state).toMatchObject({
      kind: "browser",
      variant: "web",
      url: "https://poncle.itch.io/vampire-survivors",
    });
  });
});

describe("usePlaySection — the states it must keep resolving", () => {
  it("never overrides a Steam-native game", () => {
    expect(useResolved(game({ store: "steam", store_tags: [] })).kind).toBe("steam-native");
  });

  it("never overrides when game info has not arrived", () => {
    expect(useResolved(null).kind).toBe("steam-native");
  });

  it("resolves an installed non-cloud game to installed", () => {
    expect(useResolved(game({ store: "gog", store_tags: [], is_installed: true })).kind).toBe(
      "installed",
    );
  });

  it("resolves a queued non-cloud game to downloading", () => {
    const state = useResolved(game({ store: "gog", store_tags: [] }), {
      current: null,
      queued: [{ game_id: "9NXR0000TEST", store: "gog" }],
    });
    expect(state.kind).toBe("downloading");
  });
});

/**
 * The stuck "SETTING UP GAME..." row, from the frontend side.
 *
 * The backend emits DOWNLOAD_COMPLETE before it drops the item from its
 * running map, and the store refetches the queue the moment it sees that
 * event — so it can cache a row the backend has already finished with. Since
 * nothing polls and no further event follows, that snapshot was final: the
 * `downloading` branch deliberately outranks `is_installed`, so a completed
 * install kept rendering an indeterminate progress bar whose Cancel could
 * only answer `not_found`.
 *
 * `get_queue` filters terminal rows now too. This is the second line of
 * defence, and it is the cheaper one to keep honest.
 */
describe("usePlaySection — a finished download is not an active one", () => {
  function row(over: Record<string, unknown> = {}) {
    return {
      id: "gog:2022341186",
      game_id: "2022341186",
      game_title: "BioShock™",
      store: "gog",
      status: "running",
      progress_percent: 100,
      // The phase the reported row was wedged on.
      download_phase: "preparing",
      ...over,
    };
  }

  const installed = game({
    id: "2022341186",
    store: "gog",
    store_tags: [],
    is_installed: true,
  });

  it("still shows the setup phase while the row is live", () => {
    const state = useResolved(installed, { current: row(), queued: [] });
    expect(state.kind).toBe("downloading");
  });

  it.each(["complete", "failed", "cancelled"])(
    "ignores a %s row and falls through to installed",
    (status) => {
      const state = useResolved(installed, {
        current: row({ status }),
        queued: [],
      });
      expect(state.kind).toBe("installed");
    },
  );

  it("ignores a terminal row sitting in the pending queue", () => {
    const state = useResolved(installed, {
      current: null,
      queued: [row({ status: "complete" })],
    });
    expect(state.kind).toBe("installed");
  });
});

describe("usePlaySection — backend not answering", () => {
  // `useResolved` leaves `status` alone, so reset it for the suites above.
  afterEach(() => {
    gameInfo.status = undefined;
  });

  it("hands the page back to Steam when get_game_info is unavailable", () => {
    gameInfo.status = "unavailable";
    const state = useResolved(null);
    expect(state.kind).toBe("unavailable");
    expect(state.shouldOverride).toBe(false);
  });

  it("keeps the loading state while the answer is still coming", () => {
    gameInfo.status = "loading";
    expect(useResolved(null).kind).toBe("steam-native");
  });

  it("uses data that arrived late even if the page was marked unavailable", () => {
    gameInfo.status = "unavailable";
    expect(useResolved(game()).kind).toBe("browser");
  });
});

/**
 * The Xbox PC table, through the pure resolver so each case states its own
 * status rather than leaning on the hoisted mock.
 */
describe("resolvePlaySection — Xbox PC rows", () => {
  const PID = "9PK087LNGJC5";

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
        installable: 1,
        not_supported: 0,
        choice: 0,
        skipped: 0,
        stale: false,
        last_read: null,
      },
      ...over,
    } as never;
  }

  const ready = { status: status(), loading: false, error: null };
  const disabled = { status: status({ enabled: false }), loading: false, error: null };
  const loading = { status: null, loading: true, error: null };
  const failed = { status: null, loading: false, error: "status_unavailable" };

  function pc(over: Record<string, unknown> = {}) {
    return game({
      id: PID,
      store_game_id: PID,
      store_tags: ["xbox_pc"],
      xbox_pc: { state: "ready", reason: null, access: "gamepass" },
      ...over,
    });
  }
  const merged = (over: Record<string, unknown> = {}) =>
    pc({ store_tags: ["xcloud", "browser", "xbox_pc"], ...over });

  function resolve(g: Record<string, unknown>, x: unknown, queue: unknown = null) {
    return resolvePlaySection(APP_ID, { data: g as never }, queue as never, x as never);
  }

  it("shows the skeleton while the status is still loading", () => {
    expect(resolve(pc(), loading)).toEqual({ kind: "steam-native", shouldOverride: false });
    expect(resolve(merged(), loading).kind).toBe("steam-native");
  });

  it("never shows Install when the status failed to load", () => {
    expect(resolve(pc(), failed)).toMatchObject({
      kind: "pc-unsupported",
      reason: "status_unavailable",
      installed: false,
    });
    expect(resolve(merged(), failed)).toMatchObject({ kind: "browser", variant: "stream" });
  });

  it("keeps Play on an installed row when the status failed to load", () => {
    expect(resolve(pc({ is_installed: true }), failed).kind).toBe("installed");
  });

  it("offers Install on an enabled PC-only row", () => {
    const state = resolve(pc(), ready);
    expect(state).toMatchObject({ kind: "not-installed" });
    expect("streamUrl" in state && state.streamUrl).toBeFalsy();
  });

  it("offers Install plus a secondary Stream on a merged row", () => {
    expect(resolve(merged(), ready)).toMatchObject({
      kind: "not-installed",
      streamUrl: `https://www.xbox.com/play/launch/${PID}`,
    });
  });

  it("shows Play, never Stream, on an installed merged row", () => {
    expect(resolve(merged({ is_installed: true }), ready)).toEqual({
      kind: "installed",
      shouldOverride: true,
      appId: APP_ID,
    });
  });

  it("puts a live download ahead of the xcloud tag for an xbox_pc row", () => {
    const queue = { current: { game_id: PID, store: "microsoft", status: "running" }, queued: [] };
    expect(resolve(merged(), ready, queue).kind).toBe("downloading");
    // Even with the feature off, Cancel must stay reachable.
    expect(resolve(merged(), disabled, queue).kind).toBe("downloading");
  });

  it("offers Uninstall, not Play, on an installed row with the feature off", () => {
    expect(resolve(merged({ is_installed: true }), disabled)).toMatchObject({
      kind: "pc-unsupported",
      reason: "disabled",
      installed: true,
    });
  });

  it("streams a not-installed merged row with the feature off", () => {
    expect(resolve(merged(), disabled)).toMatchObject({ kind: "browser", variant: "stream" });
    expect(resolve(merged(), disabled)).not.toHaveProperty("notice");
  });

  it("never offers Install on a PC-only row with the feature off", () => {
    expect(resolve(pc(), disabled)).toMatchObject({
      kind: "pc-unsupported",
      reason: "disabled",
      installed: false,
    });
  });

  it.each([
    ["ea_play", "ea_play"],
    ["ubisoft_plus", "ubisoft_plus"],
    ["unknown_plan", "unknown_plan"],
    ["something_new", "unavailable"],
  ])("blocks a not_supported %s row with its reason", (reason, expected) => {
    const info = { state: "not_supported", reason, access: "none" };
    expect(resolve(pc({ xbox_pc: info }), ready)).toMatchObject({
      kind: "pc-unsupported",
      reason: expected,
    });
    expect(resolve(merged({ xbox_pc: info }), ready)).toMatchObject({
      kind: "browser",
      notice: expected,
    });
  });

  it("falls back to the access field when reason is empty", () => {
    const info = { state: "not_supported", reason: null, access: "ea_play" };
    expect(resolve(pc({ xbox_pc: info }), ready)).toMatchObject({ reason: "ea_play" });
  });

  it("blocks a row that needs a package choice", () => {
    const info = { state: "choice", reason: null, access: "gamepass" };
    expect(resolve(pc({ xbox_pc: info }), ready)).toMatchObject({
      kind: "pc-unsupported",
      reason: "needs_choice",
    });
  });

  it("offers Install on a choice row whose one Game was picked", () => {
    const info = {
      state: "choice",
      reason: null,
      access: "gamepass",
      content_id: "0b3c6a7e-1d2f-4e5a-9b8c-7d6e5f4a3b2c",
    };
    expect(resolve(pc({ xbox_pc: info }), ready).kind).toBe("not-installed");
  });

  it("keys on the tag, not the store", () => {
    expect(resolve(pc({ store: "someday" }), disabled).kind).toBe("pc-unsupported");
    expect(resolve(game({ store_tags: [] }), disabled).kind).toBe("not-installed");
  });

  it("leaves an xcloud row without the tag on today's behaviour", () => {
    expect(resolve(game(), loading).kind).toBe("browser");
    expect(resolve(game(), failed).kind).toBe("browser");
  });
});
