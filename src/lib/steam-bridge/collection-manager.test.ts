// @vitest-environment jsdom
/**
 * Regression tests for the cleanup → collection-deletion flow:
 * Steam's `Delete()` mutates the live `userCollections` Map, which used
 * to skip entries mid-iteration, and the opt-in/opt-out collection manager.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";

// React and the Decky runtime are peer-provided in the Steam webview
// and absent under vitest — stub the two imports that pull them in.
// `deviceHolder` stands in for the module-level device cache. The tab
// title is derived from it at call time, exactly as production derives
// the compat tab's title from `getDeviceType()` — so if the sync fails
// to await the device type first, it names the collection from the
// stale default and the test can see it.
const deviceHolder = { current: "deck" };
vi.mock("./tab-container", () => ({
  getUnifideckTabs: () => [
    { id: "unifideck-alpha", title: "Alpha", position: 0, filters: [] },
    {
      id: "unifideck-deck",
      title: deviceHolder.current === "machine" ? "Great on Machine" : "Great on Deck",
      position: 1,
      filters: [],
    },
  ],
  isTabMasterInstalled: () => false,
}));
vi.mock("../library-filters", () => ({
  runFilters: () => true,
}));
// `call` is the only @decky/api binding reachable from here (via
// event-bus-client, which the manager subscribes to for install/uninstall).
// @decky/manifest is a build-time virtual module and unresolvable under vitest.
vi.mock("@decky/api", () => ({
  call: vi.fn(),
}));
// Collection names are translated strings, so the compat-tab keys have
// to resolve to something before they can be compared.
vi.mock("../device-type", () => ({
  COMPAT_TAB_TITLE_KEYS: [
    "deckTabs.greatOnDeck",
    "deckTabs.greatOnMachine",
    "deckTabs.steamOSCompatible",
  ],
  // Resolves late and to a NON-default device, so a sync that failed to
  // await it would be caught naming things "Great on Deck".
  // Resolves late and to a NON-default device, flipping the holder as it
  // lands. A sync that skips this await sees "deck" and is caught.
  awaitDeviceType: () =>
    new Promise((r) =>
      setTimeout(() => {
        deviceHolder.current = "machine";
        r("machine");
      }, 5),
    ),
}));
vi.mock("i18next", () => ({
  default: {
    t: (key: string) =>
      ({
        "deckTabs.greatOnDeck": "Great on Deck",
        "deckTabs.greatOnMachine": "Great on Machine",
        "deckTabs.steamOSCompatible": "SteamOS Compatible",
      }[key] ?? key),
  },
}));

import {
  deleteAllUnifideckCollections,
  syncUnifideckCollections,
  startCollectionManager,
} from "./collection-manager";

const COLLECTIONS_ENABLED_KEY = "unifideck:collections.enabled";
const COLLECTIONS_CLEANED_KEY = "unifideck:collections.cleaned";

interface MockCollection {
  id: string;
  displayName: string;
  allApps: unknown[];
  AsDragDropCollection: () => {
    AddApps: (o: unknown[]) => void;
    RemoveApps: (o: unknown[]) => void;
  };
  Save: () => Promise<void>;
  Delete: () => Promise<void>;
}

function makeStore(names: string[]) {
  const map = new Map<string, MockCollection>();
  let nextId = 1;
  const make = (name: string): MockCollection => {
    const id = `c${nextId++}`;
    const c: MockCollection = {
      id,
      displayName: name,
      allApps: [],
      AsDragDropCollection: () => ({ AddApps: () => {}, RemoveApps: () => {} }),
      Save: async () => {},
      // Mutates the backing Map mid-iteration — the exact behavior
      // that made the old live-iterator deletion skip entries.
      Delete: async () => {
        map.delete(id);
      },
    };
    map.set(id, c);
    return c;
  };
  names.forEach(make);
  const store = {
    userCollections: map,
    GetCollection: vi.fn((id: string) =>
      id === "type-games" ? { allApps: [{ appid: 1, display_name: "Game" }] } : map.get(id),
    ),
    GetCollectionIDByUserTag: vi.fn((tag: string) => {
      for (const c of map.values()) if (c.displayName === tag) return c.id;
      return null;
    }),
    NewUnsavedCollection: vi.fn((tag: string) => make(tag)),
  };
  (window as unknown as { collectionStore: unknown }).collectionStore = store;
  (window as unknown as { appStore: unknown }).appStore = {
    GetAppOverviewByAppID: () => ({ appid: 1, display_name: "Game" }),
  };
  return { map, store };
}

beforeEach(() => {
  window.localStorage.clear();
});

describe("deleteAllUnifideckCollections", () => {
  it("deletes every [Unifideck] collection despite Map mutation during Delete()", async () => {
    const { map } = makeStore([
      "[Unifideck] Alpha",
      "[Unifideck] Beta",
      "Untouched",
      "[Unifideck] Gamma",
      "[Unifideck] Delta",
    ]);
    await deleteAllUnifideckCollections();
    const remaining = Array.from(map.values()).map((c) => c.displayName);
    expect(remaining).toEqual(["Untouched"]);
  });

  it("does not sync collections when disabled", async () => {
    const { store } = makeStore(["[Unifideck] Alpha"]);
    window.localStorage.setItem(COLLECTIONS_ENABLED_KEY, "0");

    store.GetCollection.mockClear();
    store.NewUnsavedCollection.mockClear();
    await syncUnifideckCollections();

    expect(store.GetCollection).not.toHaveBeenCalled();
    expect(store.NewUnsavedCollection).not.toHaveBeenCalled();
  });
});

describe("cross-device compat collections", () => {
  /**
   * Collections are account-global and cloud-synced, but the compat
   * tab is named after the local device. Without the valid-name union,
   * a user's Deck and Steam Machine delete each other's compat
   * collection on every boot, forever.
   */
  it("keeps the other devices' compat collections", async () => {
    const { map } = makeStore([
      "[Unifideck] Alpha",
      "[Unifideck] Great on Deck",
      "[Unifideck] Great on Machine",
      "[Unifideck] SteamOS Compatible",
    ]);
    window.localStorage.setItem(COLLECTIONS_ENABLED_KEY, "1");

    await syncUnifideckCollections();

    const names = Array.from(map.values()).map((c) => c.displayName);
    expect(names).toContain("[Unifideck] Great on Deck");
    expect(names).toContain("[Unifideck] Great on Machine");
    expect(names).toContain("[Unifideck] SteamOS Compatible");
  });

  it("still deletes a genuinely stale [Unifideck] collection", async () => {
    const { map } = makeStore([
      "[Unifideck] Alpha",
      "[Unifideck] Great on Machine",
      "[Unifideck] Some Removed Tab",
    ]);
    window.localStorage.setItem(COLLECTIONS_ENABLED_KEY, "1");

    await syncUnifideckCollections();

    const names = Array.from(map.values()).map((c) => c.displayName);
    expect(names).not.toContain("[Unifideck] Some Removed Tab");
    expect(names).toContain("[Unifideck] Great on Machine");
  });
});

describe("startCollectionManager", () => {
  it("runs cleanup once when collections are disabled on startup", async () => {
    const { map } = makeStore(["[Unifideck] Alpha"]);
    window.localStorage.setItem(COLLECTIONS_ENABLED_KEY, "0");

    const handle = startCollectionManager();

    // Wait for the async waitForCollections() promise chain to resolve
    await new Promise((r) => setTimeout(r, 10));

    expect(window.localStorage.getItem(COLLECTIONS_CLEANED_KEY)).toBe("1");
    expect(Array.from(map.values()).map((c) => c.displayName)).not.toContain("[Unifideck] Alpha");

    handle.remove();
  });
});

describe("device-type race", () => {
  /**
   * Regression guard for a real defect found in adversarial review.
   * `startCollectionManager()` runs at plugin init, before the
   * device-type RPC answers. Collection names are device-specific AND
   * account-global + cloud-synced, so naming one from the cached
   * default would push a wrong-device collection to every device on the
   * account — where the all-three-names whitelist then protects it from
   * cleanup forever.
   */
  it("waits for the device type before naming a collection", async () => {
    deviceHolder.current = "deck"; // the stale cached default
    const { map } = makeStore([]);
    window.localStorage.setItem(COLLECTIONS_ENABLED_KEY, "1");

    await syncUnifideckCollections();

    const names = Array.from(map.values()).map((c) => c.displayName);
    // The device resolves to "machine", so that is the only compat
    // collection that may exist. Seeing the Deck name means the sync
    // named it from the default before the RPC answered — which would
    // then cloud-sync to every device on the account.
    expect(names).toContain("[Unifideck] Great on Machine");
    expect(names).not.toContain("[Unifideck] Great on Deck");
  });
});
