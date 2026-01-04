/**
 * Unifideck Tab System
 * 
 * Exports the tab system for use in the main plugin.
 */

export { patchLibrary } from './LibraryPatch';
export { tabManager, UnifideckTabContainer, UNIFIDECK_TABS, HIDDEN_DEFAULT_TABS } from './TabContainer';
export { updateUnifideckCache, runFilter, runFilters, isUnifideckGame, getStoreForApp, unifideckGameCache } from './filters';
export type { TabFilter, FilterType, FilterParams } from './filters';
export { getProtonDBRating, getCachedRating, getCompatByTitle, getCachedCompatByTitle, prefetchCompatByTitles, meetsGreatOnDeckCriteria } from './protondb';
export type { ProtonDBTier, DeckVerifiedStatus, GameCompatInfo } from './protondb';
