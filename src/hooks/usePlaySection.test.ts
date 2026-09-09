/**
 * The one `if` that keeps the cloud-only store out of the install path.
 *
 * Audit §3.5 bullet 1. A Microsoft/xCloud title has nothing to install, and
 * the backend now refuses install / update / uninstall outright. But the
 * reason a user never sees an Install button for one is this hook: an
 * `xcloud`-tagged game resolves to its own play state *before* the
 * `not-installed` branch, so `NotInstalledButtons` — the only Install entry
 * point in the UI — never mounts.
 *
 * That guard had no test. `store_info.supports_install` does not provide one
 * either: it has no readers anywhere (register item 26), so it gates nothing.
 * The next Microsoft feature — PC Game Pass, i.e. titles that really do
 * install — is exactly a Microsoft game *without* the tag, so the branch that
 * must keep working is the ordering between these two.
 */
import { describe, it, expect, vi } from "vitest";

const gameInfo = vi.hoisted(() => ({ data: null as unknown }));
const downloads = vi.hoisted(() => ({ queue: null as unknown }));

vi.mock("./useGameInfo", () => ({ useGameInfo: () => gameInfo }));
vi.mock("../contexts/DownloadContext", () => ({
  useDownloads: () => downloads,
}));

import { usePlaySection } from "./usePlaySection";

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
  it("resolves an xcloud game to the xcloud state, not not-installed", () => {
    const state = useResolved(game());
    expect(state.kind).toBe("xcloud");
  });

  it("does the same when the store is not microsoft", () => {
    // The branch keys on the tag, not the store name — a second
    // subscription store would inherit the guard for free, and a
    // store-name check here would silently not.
    expect(useResolved(game({ store: "someday" })).kind).toBe("xcloud");
  });

  it("still offers install for a Microsoft game WITHOUT the tag", () => {
    // Not a wish: this is what PC Game Pass support would look like, and
    // it is why the backend refusal exists rather than relying on this
    // hook. If this ever becomes reachable for real, the store must be
    // the thing that says no.
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
    expect(state.kind).toBe("xcloud");
  });

  it("takes precedence over is_installed in either direction", () => {
    expect(useResolved(game({ is_installed: true })).kind).toBe("xcloud");
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
