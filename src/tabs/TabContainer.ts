/**
 * Unifideck Tab Container
 * 
 * Manages custom tabs for the Steam library that include
 * Epic, GOG, and Amazon games alongside Steam games.
 */

import { TabFilter, runFilters, updateUnifideckCache, unifideckGameCache } from './filters';
import { prefetchCompatByTitles } from './protondb';
import { gamepadTabbedPageClasses } from '@decky/ui';
import { call } from '@decky/api';
import React, { ReactElement } from 'react';

// Tab definition
export interface UnifideckTab {
    id: string;
    title: string;
    position: number;
    filters: TabFilter[];
    icon?: string;
}

// Default Unifideck tabs - ORDERED: Great on Deck, All Games, Installed, Steam, Epic, GOG, Amazon, Non-Steam
export const UNIFIDECK_TABS: UnifideckTab[] = [
    {
        id: 'unifideck-deck',
        title: 'Great on Deck',
        position: 0,
        filters: [{ type: 'deckCompat', params: {} }]  // Native, Platinum, or Verified only
    },
    {
        id: 'unifideck-all',
        title: 'All Games',  // Renamed from "All"
        position: 1,
        filters: [{ type: 'all', params: {} }]
    },
    {
        id: 'unifideck-installed',
        title: 'Installed',
        position: 2,
        filters: [{ type: 'installed', params: { installed: true } }]
    },
    {
        id: 'unifideck-steam',
        title: 'Steam',
        position: 3,
        filters: [{ type: 'store', params: { store: 'steam' } }]
    },
    {
        id: 'unifideck-epic',
        title: 'Epic',
        position: 4,
        filters: [{ type: 'store', params: { store: 'epic' } }]
    },
    {
        id: 'unifideck-gog',
        title: 'GOG',
        position: 5,
        filters: [{ type: 'store', params: { store: 'gog' } }]
    },
    {
        id: 'unifideck-amazon',
        title: 'Amazon',
        position: 6,
        filters: [{ type: 'store', params: { store: 'amazon' } }]
    },
    {
        id: 'unifideck-nonsteam',
        title: 'Non-Steam',
        position: 7,
        filters: [{ type: 'nonSteam', params: {} }]  // All non-Steam shortcuts except non-installed Unifideck
    }
];

// IDs of default Steam tabs to hide (when TabMaster is NOT present)
// Note: Non-Steam tab is called 'DesktopApps' internally by Steam!
const DEFAULT_TABS_TO_HIDE = [
    'GreatOnDeck',
    'AllGames',
    'Installed',
    'DesktopApps',   // This is Steam's actual ID for the Non-Steam tab!
];

/**
 * Check if TabMaster plugin is installed
 */
export function isTabMasterInstalled(): boolean {
    try {
        const plugins = (window as any).DeckyPluginLoader?.plugins ?? [];
        return plugins.some((p: any) => p.name === 'TabMaster' || p.name === 'Tab Master');
    } catch {
        return false;
    }
}

/**
 * Get tabs to hide based on TabMaster presence
 * If TabMaster is present, we don't hide any tabs (user can manage via TabMaster)
 */
export function getHiddenDefaultTabs(): string[] {
    if (isTabMasterInstalled()) {
        console.log('[Unifideck] TabMaster detected - not hiding default tabs');
        return []; // Don't hide any tabs, let TabMaster manage
    }
    return DEFAULT_TABS_TO_HIDE;
}

// Export for backward compatibility
export const HIDDEN_DEFAULT_TABS = DEFAULT_TABS_TO_HIDE;

/**
 * Custom Tab Container for Unifideck
 * Builds a filtered collection of games for each tab
 */
export class UnifideckTabContainer {
    id: string;
    title: string;
    position: number;
    filters: TabFilter[];
    collection: any;

    constructor(tab: UnifideckTab) {
        this.id = tab.id;
        this.title = tab.title;
        this.position = tab.position;
        this.filters = tab.filters;

        // Initialize collection structure
        this.collection = {
            AsDeletableCollection: () => null,
            AsDragDropCollection: () => null,
            AsEditableCollection: () => null,
            GetAppCountWithToolsFilter: (appFilter: any) =>
                this.collection.visibleApps.filter((app: SteamAppOverview) => appFilter.Matches(app)).length,
            bAllowsDragAndDrop: false,
            bIsDeletable: false,
            bIsDynamic: false,
            bIsEditable: false,
            displayName: this.title,
            id: this.id,
            allApps: [] as SteamAppOverview[],
            visibleApps: [] as SteamAppOverview[],
            apps: new Map<number, SteamAppOverview>()
        };

        this.buildCollection();
    }

    /**
     * Builds the filtered collection of apps for this tab
     */
    buildCollection() {
        try {
            // Get all games from Steam's collection store
            const allGamesCollection = (window as any).collectionStore?.GetCollection('type-games');
            if (!allGamesCollection) {
                console.log('[Unifideck] Could not access collectionStore');
                return;
            }

            const allApps = allGamesCollection.allApps || [];

            // Filter apps based on tab filters
            const filteredApps = allApps.filter((app: SteamAppOverview) =>
                runFilters(this.filters, app)
            );

            this.collection.allApps = filteredApps;
            this.collection.visibleApps = [...filteredApps];

            // Build apps map
            const appMap = new Map<number, SteamAppOverview>();
            filteredApps.forEach((app: SteamAppOverview) => {
                appMap.set(app.appid, app);
            });
            this.collection.apps = appMap;

            console.log(`[Unifideck] Tab "${this.title}" has ${filteredApps.length} games`);
        } catch (error) {
            console.error('[Unifideck] Error building collection:', error);
        }
    }

    /**
     * Gets the SteamTab object for injection into the library
     */
    getActualTab(
        TabAppGrid: any,
        TabContext: any,
        sortingProps: any,
        collectionAppFilter: any
    ): SteamTab | null {
        // Rebuild collection to ensure fresh data
        this.buildCollection();

        // Create the tab content
        const createContent = (inner: ReactElement) =>
            TabContext
                ? React.createElement(TabContext.Provider, { value: { label: this.title } }, inner)
                : inner;

        return {
            title: this.title,
            id: this.id,
            footer: {},
            content: createContent(
                React.createElement(TabAppGrid, {
                    collection: this.collection,
                    setSortBy: sortingProps.setSortBy,
                    eSortBy: sortingProps.eSortBy,
                    showSortingContextMenu: sortingProps.showSortingContextMenu
                })
            ),
            renderTabAddon: () => {
                return React.createElement('span',
                    { className: gamepadTabbedPageClasses?.TabCount || '' },
                    this.collection.GetAppCountWithToolsFilter(collectionAppFilter)
                );
            }
        };
    }
}

// Tab manager singleton
class TabManager {
    private tabs: UnifideckTabContainer[] = [];
    private initialized = false;
    private cacheLoaded = false;
    private epicGameCount = 0;
    private gogGameCount = 0;
    private amazonGameCount = 0;

    async initialize() {
        if (this.initialized) return;

        // Load game cache from backend
        await this.loadGameCache();

        this.tabs = UNIFIDECK_TABS.map(tab => new UnifideckTabContainer(tab));
        this.initialized = true;
        console.log('[Unifideck] TabManager initialized with', this.tabs.length, 'tabs');
    }

    /**
     * Load game cache from backend
     */
    async loadGameCache(): Promise<void> {
        if (this.cacheLoaded) return;

        try {
            console.log('[Unifideck] Loading game cache from backend...');
            const games = await call<[], any>('get_all_unifideck_games');

            if (Array.isArray(games) && games.length > 0) {
                const cacheData = games.map(g => ({
                    appId: g.appId,
                    store: g.store as 'epic' | 'gog' | 'amazon',
                    isInstalled: g.isInstalled
                }));
                updateUnifideckCache(cacheData);

                // Count games by store for tab visibility
                this.epicGameCount = games.filter((g: any) => g.store === 'epic').length;
                this.gogGameCount = games.filter((g: any) => g.store === 'gog').length;
                this.amazonGameCount = games.filter((g: any) => g.store === 'amazon').length;
                console.log(`[Unifideck] Loaded ${games.length} games into cache (Epic: ${this.epicGameCount}, GOG: ${this.gogGameCount}, Amazon: ${this.amazonGameCount})`);

                // Prefetch compatibility info (ProtonDB + Deck Verified) for Epic/GOG/Amazon games
                const titles = games
                    .filter((g: any) => g.title)
                    .map((g: any) => g.title);
                if (titles.length > 0) {
                    console.log(`[Unifideck] Prefetching compatibility for ${titles.length} games...`);
                    // Run in background - don't block tab initialization
                    prefetchCompatByTitles(titles).catch((err: Error) =>
                        console.error('[Unifideck] Compat prefetch error:', err)
                    );
                }

                this.cacheLoaded = true;
            } else {
                console.log('[Unifideck] No Unifideck games found in backend');
            }
        } catch (error) {
            console.error('[Unifideck] Error loading game cache:', error);
        }
    }

    getTabs(): UnifideckTabContainer[] {
        return this.tabs.filter(tab => this.shouldShowTab(tab.id));
    }

    /**
     * Determines if a tab should be visible based on game availability
     */
    private shouldShowTab(tabId: string): boolean {
        if (tabId === 'unifideck-epic' && this.epicGameCount === 0) {
            return false;
        }
        if (tabId === 'unifideck-gog' && this.gogGameCount === 0) {
            return false;
        }
        if (tabId === 'unifideck-amazon' && this.amazonGameCount === 0) {
            return false;
        }
        return true;
    }

    isInitialized(): boolean {
        return this.initialized;
    }

    rebuildTabs() {
        this.tabs.forEach(tab => tab.buildCollection());
    }

    /**
     * Updates the game cache with Unifideck game info
     */
    updateGameCache(games: Array<{ appId: number; store: 'epic' | 'gog' | 'amazon'; isInstalled: boolean }>) {
        updateUnifideckCache(games);
        this.cacheLoaded = true;
        this.rebuildTabs();
    }
}

export const tabManager = new TabManager();

// Type for Steam tabs
interface SteamTab {
    title: string;
    id: string;
    content: ReactElement;
    footer: any;
    renderTabAddon?: () => ReactElement;
}
