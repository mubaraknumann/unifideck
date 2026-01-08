/**
 * CollectionManager.ts
 * 
 * Manages Steam Collections for Unifideck games.
 * 
 * Features:
 * - Auto-generates collections from UNIFIDECK_TABS definitions
 * - Deduplicates and cleans up old/stale Unifideck collections
 * - Clears and rebuilds collections on each sync for accuracy
 */

import { UNIFIDECK_TABS, UnifideckTab } from '../tabs/TabContainer';
import { runFilters } from '../tabs/filters';

// Prefix for all Unifideck collections
const COLLECTION_PREFIX = '[Unifideck] ';

// Type definitions
interface AppStoreOverview {
    appid: number;
    display_name: string;
    app_type?: number;
    installed?: boolean;
    steam_deck_compat_category?: number;
}

interface Collection {
    AsDragDropCollection: () => {
        AddApps: (overviews: AppStoreOverview[]) => void;
        RemoveApps: (overviews: AppStoreOverview[]) => void;
    };
    Save: () => Promise<void>;
    Delete: () => Promise<void>;
    allApps: AppStoreOverview[];
    apps: {
        keys: () => IterableIterator<number>;
        has: (appId: number) => boolean;
    };
    displayName: string;
    id: string;
}

interface CollectionStore {
    GetCollection: (collectionId: string) => Collection | undefined;
    GetCollectionIDByUserTag: (tag: string) => string | null;
    NewUnsavedCollection: (tag: string, filter: unknown | undefined, overviews: AppStoreOverview[]) => Collection | undefined;
    userCollections: Collection[];
}

interface AppStoreEx {
    getAppOverview: (appId: number) => AppStoreOverview | null;
}

/**
 * Get collectionStore from window
 */
function getCollectionStore(): CollectionStore | null {
    return (window as any).collectionStore ?? null;
}

/**
 * Get appStore for app overviews
 */
function getAppStoreEx(): AppStoreEx | null {
    const appStore = (window as any).appStore;
    if (!appStore) return null;

    return {
        getAppOverview: (appId: number) => {
            try {
                return appStore.GetAppOverviewByAppID(appId) ?? null;
            } catch {
                return null;
            }
        }
    };
}

/**
 * Get all apps from Steam's collection
 */
function getAllApps(): AppStoreOverview[] {
    console.log('[Unifideck Collections] Getting all apps from type-games...');
    const collectionStore = getCollectionStore();
    if (!collectionStore) {
        console.error('[Unifideck Collections] collectionStore is null');
        return [];
    }

    try {
        const typeGames = collectionStore.GetCollection('type-games');
        if (!typeGames) {
            console.error('[Unifideck Collections] type-games collection is null');
            return [];
        }
        const apps = typeGames.allApps ?? [];
        console.log(`[Unifideck Collections] Found ${apps.length} apps in type-games`);
        return apps;
    } catch (e) {
        console.error('[Unifideck Collections] Error getting type-games:', e);
        return [];
    }
}

/**
 * Convert tab title to collection name
 */
function tabToCollectionName(tab: UnifideckTab): string {
    return `${COLLECTION_PREFIX}${tab.title}`;
}

/**
 * Get all valid Unifideck collection names (from current UNIFIDECK_TABS)
 */
function getValidCollectionNames(): Set<string> {
    return new Set(UNIFIDECK_TABS.map(tabToCollectionName));
}

/**
 * Delete a collection by ID
 */
async function deleteCollection(collection: Collection): Promise<void> {
    try {
        await collection.Delete();
        console.log(`[Unifideck Collections] Deleted stale collection: ${collection.displayName}`);
    } catch (e) {
        console.error(`[Unifideck Collections] Failed to delete ${collection.displayName}:`, e);
    }
}

/**
 * Clean up old/stale Unifideck collections that don't match current tabs
 */
async function cleanupStaleCollections(): Promise<void> {
    const collectionStore = getCollectionStore();
    if (!collectionStore) return;

    // Safety check: userCollections may not exist or may throw if Steam isn't fully initialized
    // The getter internally calls .values() on a Map which can be undefined during early init
    let userCollections: Collection[] | null = null;
    try {
        userCollections = collectionStore.userCollections;
    } catch (e) {
        console.log('[Unifideck Collections] Error accessing userCollections, skipping cleanup:', e);
        return;
    }

    if (!userCollections || !Array.isArray(userCollections)) {
        console.log('[Unifideck Collections] userCollections not available, skipping cleanup');
        return;
    }

    const validNames = getValidCollectionNames();
    console.log(`[Unifideck Collections] Checking ${userCollections.length} user collections for stale entries...`);

    for (const collection of userCollections) {
        // Check if this is a Unifideck collection
        if (collection?.displayName?.startsWith(COLLECTION_PREFIX)) {
            // Check if it's NOT in the valid list (stale)
            if (!validNames.has(collection.displayName)) {
                await deleteCollection(collection);
            }
        }
    }
}

/**
 * Get or create a collection by name/tag
 */
async function getOrCreateCollection(tag: string): Promise<Collection | null> {
    const collectionStore = getCollectionStore();
    if (!collectionStore) {
        console.error(`[Unifideck Collections] collectionStore not available`);
        return null;
    }

    // Check if collection already exists
    const collectionId = collectionStore.GetCollectionIDByUserTag(tag);
    if (typeof collectionId === 'string') {
        const collection = collectionStore.GetCollection(collectionId);
        if (collection) {
            return collection;
        }
    }

    // Create new collection
    const collection = collectionStore.NewUnsavedCollection(tag, undefined, []);
    if (!collection) {
        console.error(`[Unifideck Collections] Failed to create collection: ${tag}`);
        return null;
    }

    await collection.Save();
    console.log(`[Unifideck Collections] Created new collection: ${tag}`);
    return collection;
}

/**
 * Clear all apps from a collection
 */
async function clearCollection(collection: Collection): Promise<void> {
    const existingApps = collection.allApps ?? [];
    if (existingApps.length > 0) {
        collection.AsDragDropCollection().RemoveApps(existingApps);
        await collection.Save();
    }
}

/**
 * Sync a single tab's apps to its corresponding collection
 */
async function syncTabToCollection(tab: UnifideckTab, allApps: AppStoreOverview[]): Promise<boolean> {
    const collectionName = tabToCollectionName(tab);

    // Filter apps using the SAME logic as custom tabs
    const matchingApps = allApps.filter(app => {
        if (app.appid <= 0) return false;
        return runFilters(tab.filters, app as any);
    });

    const collection = await getOrCreateCollection(collectionName);
    if (!collection) {
        return false;
    }

    const appStoreEx = getAppStoreEx();
    if (!appStoreEx) {
        console.error(`[Unifideck Collections] appStore not available`);
        return false;
    }

    // Clear existing apps and rebuild (ensures no stale entries)
    await clearCollection(collection);

    // Get app overviews for matching apps
    const overviews: AppStoreOverview[] = [];
    for (const app of matchingApps) {
        const overview = appStoreEx.getAppOverview(app.appid);
        if (overview) {
            overviews.push(overview);
        }
    }

    if (overviews.length > 0) {
        collection.AsDragDropCollection().AddApps(overviews);
        await collection.Save();
    }

    console.log(`[Unifideck Collections] "${collectionName}": ${overviews.length} apps`);
    return true;
}

/**
 * Sync all Unifideck collections.
 * 
 * 1. Cleans up old/stale collections
 * 2. Auto-generates a collection for each tab in UNIFIDECK_TABS
 * 3. Clears and rebuilds each collection for accuracy
 */
export async function syncUnifideckCollections(): Promise<void> {
    console.log('[Unifideck Collections] Starting collection sync...');
    console.log(`[Unifideck Collections] Syncing ${UNIFIDECK_TABS.length} tabs to collections`);

    // Step 1: Clean up stale collections
    await cleanupStaleCollections();

    // Step 2: Get all apps
    const allApps = getAllApps();
    if (allApps.length === 0) {
        console.warn('[Unifideck Collections] No apps found, aborting sync');
        return;
    }

    // Step 3: Sync each tab to its corresponding collection
    const results = await Promise.allSettled(
        UNIFIDECK_TABS.map(tab => syncTabToCollection(tab, allApps))
    );

    const succeeded = results.filter(r => r.status === 'fulfilled' && r.value).length;
    console.log(`[Unifideck Collections] ✓ Sync complete: ${succeeded}/${UNIFIDECK_TABS.length} collections updated`);
}

/**
 * Check if collections feature is available
 */
export function isCollectionsAvailable(): boolean {
    const store = getCollectionStore();
    return store !== null &&
        typeof store.GetCollectionIDByUserTag === 'function' &&
        typeof store.NewUnsavedCollection === 'function';
}
