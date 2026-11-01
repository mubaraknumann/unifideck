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
// `filterHolder` decides what every tab matches; `cacheGate` stands in for
// the game-cache load the sync must wait for. Both are read at call time,
// never at mock-factory time, so the hoisted factory can close over them.
const filterHolder = { matches: true };
const cacheGate: { promise: Promise<void> } = { promise: Promise.resolve() };
// The compat tab's caches load on their own RPCs; the sync awaits them.
vi.mock("../library-facets", () => ({ loadFacets: async () => {} }));
vi.mock("../protondb-cache", () => ({ loadCompatCacheFromBackend: async () => {} }));
vi.mock("../library-filters", () => ({
  runFilters: () => filterHolder.matches,
  whenUnifideckCacheLoaded: () => cacheGate.promise,
  UNIFIDECK_CACHE_UPDATED_EVENT: "unifideck-cache-updated",
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
  collectSteamOwnedGames,
  deleteAllUnifideckCollections,
  syncUnifideckCollections,
  startCollectionManager,
} from "./collection-manager";

const COLLECTIONS_ENABLED_KEY = "unifideck:collections.enabled";
const COLLECTIONS_CLEANED_KEY = "unifideck:collections.cleaned";

interface MockApp {
  appid: number;
  display_name: string;
}

interface MockCollection {
  id: string;
  displayName: string;
  allApps: MockApp[];
  saves: number;
  AsDragDropCollection: () => {
    AddApps: (o: MockApp[]) => void;
    RemoveApps: (o: MockApp[]) => void;
  };
  Save: () => Promise<void>;
  Delete: () => Promise<void>;
}

const game = (appid: number): MockApp => ({ appid, display_name: `Game ${appid}` });

function makeStore(names: string[], games: MockApp[] = [game(1)]) {
  const map = new Map<string, MockCollection>();
  let nextId = 1;
  const make = (name: string): MockCollection => {
    const id = `c${nextId++}`;
    const c: MockCollection = {
      id,
      displayName: name,
      allApps: [],
      saves: 0,
      AsDragDropCollection: () => ({
        AddApps: (o) => {
          c.allApps = [...c.allApps, ...o];
        },
        RemoveApps: (o) => {
          const gone = new Set(o.map((a) => a.appid));
          c.allApps = c.allApps.filter((a) => !gone.has(a.appid));
        },
      }),
      Save: async () => {
        c.saves += 1;
      },
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
    GetCollection: vi.fn((id: string) => (id === "type-games" ? { allApps: games } : map.get(id))),
    GetCollectionIDByUserTag: vi.fn((tag: string) => {
      for (const c of map.values()) if (c.displayName === tag) return c.id;
      return null;
    }),
    NewUnsavedCollection: vi.fn((tag: string) => make(tag)),
  };
  (window as unknown as { collectionStore: unknown }).collectionStore = store;
  (window as unknown as { appStore: unknown }).appStore = {
    GetAppOverviewByAppID: (id: number) => games.find((g) => g.appid === id) ?? null,
  };
  return { map, store, games };
}

// This vitest/jsdom combination's `window.localStorage` doesn't implement
// `getItem`/`setItem`/`clear` (a known limitation — see the same
// workaround note in the sibling `library-filters/index.test.ts`). That
// file's production code sits behind a mockable module boundary; this
// suite calls `window.localStorage` directly, so it needs a real,
// working store rather than a mocked-away import — a minimal in-memory
// polyfill, installed once and reset in `beforeEach`.
const memoryStorage = new Map<string, string>();
Object.defineProperty(window, "localStorage", {
  configurable: true,
  value: {
    getItem: (key: string) => memoryStorage.get(key) ?? null,
    setItem: (key: string, value: string) => {
      memoryStorage.set(key, String(value));
    },
    removeItem: (key: string) => {
      memoryStorage.delete(key);
    },
    clear: () => {
      memoryStorage.clear();
    },
    key: (index: number) => Array.from(memoryStorage.keys())[index] ?? null,
    get length() {
      return memoryStorage.size;
    },
  },
});

beforeEach(() => {
  window.localStorage.clear();
  filterHolder.matches = true;
  cacheGate.promise = Promise.resolve();
});

const byName = (map: Map<string, MockCollection>, name: string) =>
  Array.from(map.values()).find((c) => c.displayName === name);

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

describe("collectSteamOwnedGames", () => {
  function app(appid: number, title: string, extra: object = {}): object {
    return { appid, display_name: title, app_type: 1, ...extra };
  }

  function withGames(apps: object[]): void {
    (window as unknown as { collectionStore: unknown }).collectionStore = {
      GetCollection: () => ({ allApps: apps }),
    };
  }

  it("lists owned games and skips a game borrowed through Family Sharing", () => {
    withGames([
      app(10, "Owned", { BIsOwned: () => true, BIsBorrowed: () => false }),
      app(20, "Borrowed", { BIsOwned: () => false, BIsBorrowed: () => true }),
      app(30, "Older client"),
    ]);

    expect(collectSteamOwnedGames()).toEqual([
      { title: "Owned", appid: 10 },
      { title: "Older client", appid: 30 },
    ]);
  });
});

describe("collection ids stay stable for TabMaster", () => {
  /**
   * TabMaster stores a collection filter as the collection's id. Steam gives
   * a re-created collection a new id, so deleting one broke every TabMaster
   * tab built on it ("Collection: [Unifideck] Epic no longer exists").
   */
  it("empties a collection whose tab matches nothing instead of deleting it", async () => {
    const { map, store } = makeStore(["[Unifideck] Alpha"]);
    const alpha = byName(map, "[Unifideck] Alpha")!;
    alpha.allApps = [game(1)];
    window.localStorage.setItem(COLLECTIONS_ENABLED_KEY, "1");
    filterHolder.matches = false;

    await syncUnifideckCollections();

    expect(byName(map, "[Unifideck] Alpha")?.id).toBe(alpha.id);
    expect(alpha.allApps).toEqual([]);
    // ...and still never creates an empty shell for a tab with no games.
    expect(store.NewUnsavedCollection).not.toHaveBeenCalled();
  });

  it("does not sync until the game cache has loaded", async () => {
    const { map, store } = makeStore([]);
    window.localStorage.setItem(COLLECTIONS_ENABLED_KEY, "1");
    let release!: () => void;
    cacheGate.promise = new Promise<void>((r) => {
      release = r;
    });

    const sync = syncUnifideckCollections();
    // Well past the 5 ms device-type await: only the cache gate is holding it.
    await new Promise((r) => setTimeout(r, 30));
    expect(store.NewUnsavedCollection).not.toHaveBeenCalled();

    release();
    await sync;
    expect(byName(map, "[Unifideck] Alpha")?.allApps.map((a) => a.appid)).toEqual([1]);
  });
});

describe("collection updates", () => {
  it("adds and removes only what changed, with one save, and skips a no-op", async () => {
    const { map } = makeStore(["[Unifideck] Alpha"], [game(2), game(3)]);
    const alpha = byName(map, "[Unifideck] Alpha")!;
    alpha.allApps = [game(1), game(2)];
    window.localStorage.setItem(COLLECTIONS_ENABLED_KEY, "1");

    await syncUnifideckCollections();
    expect(alpha.allApps.map((a) => a.appid).sort()).toEqual([2, 3]);
    expect(alpha.saves).toBe(1);

    await syncUnifideckCollections();
    expect(alpha.saves).toBe(1);
  });

  it("rebuilds when the game cache reloads", async () => {
    const { map, games } = makeStore([]);
    window.localStorage.setItem(COLLECTIONS_ENABLED_KEY, "1");
    const handle = startCollectionManager();
    try {
      await new Promise((r) => setTimeout(r, 30));
      expect(byName(map, "[Unifideck] Alpha")?.allApps.map((a) => a.appid)).toEqual([1]);

      games.push(game(5));
      window.dispatchEvent(new CustomEvent("unifideck-cache-updated"));
      await new Promise((r) => setTimeout(r, 30));

      expect(byName(map, "[Unifideck] Alpha")?.allApps.map((a) => a.appid)).toEqual([1, 5]);
    } finally {
      handle.remove();
    }
  });
});
