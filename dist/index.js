const manifest = {"name":"Unifideck"};
const API_VERSION = 2;
const internalAPIConnection = window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit;
if (!internalAPIConnection) {
    throw new Error('[@decky/api]: Failed to connect to the loader as as the loader API was not initialized. This is likely a bug in Decky Loader.');
}
let api;
try {
    api = internalAPIConnection.connect(API_VERSION, manifest.name);
}
catch {
    api = internalAPIConnection.connect(1, manifest.name);
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version 1. Some features may not work.`);
}
if (api._version != API_VERSION) {
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version ${api._version}. Some features may not work.`);
}
const call = api.call;
const routerHook = api.routerHook;
const toaster = api.toaster;
const fetchNoCors = api.fetchNoCors;
const definePlugin = (fn) => {
    return (...args) => {
        return fn(...args);
    };
};

var DefaultContext = {
  color: undefined,
  size: undefined,
  className: undefined,
  style: undefined,
  attr: undefined
};
var IconContext = SP_REACT.createContext && /*#__PURE__*/SP_REACT.createContext(DefaultContext);

var _excluded = ["attr", "size", "title"];
function _objectWithoutProperties(source, excluded) { if (source == null) return {}; var target = _objectWithoutPropertiesLoose(source, excluded); var key, i; if (Object.getOwnPropertySymbols) { var sourceSymbolKeys = Object.getOwnPropertySymbols(source); for (i = 0; i < sourceSymbolKeys.length; i++) { key = sourceSymbolKeys[i]; if (excluded.indexOf(key) >= 0) continue; if (!Object.prototype.propertyIsEnumerable.call(source, key)) continue; target[key] = source[key]; } } return target; }
function _objectWithoutPropertiesLoose(source, excluded) { if (source == null) return {}; var target = {}; for (var key in source) { if (Object.prototype.hasOwnProperty.call(source, key)) { if (excluded.indexOf(key) >= 0) continue; target[key] = source[key]; } } return target; }
function _extends() { _extends = Object.assign ? Object.assign.bind() : function (target) { for (var i = 1; i < arguments.length; i++) { var source = arguments[i]; for (var key in source) { if (Object.prototype.hasOwnProperty.call(source, key)) { target[key] = source[key]; } } } return target; }; return _extends.apply(this, arguments); }
function ownKeys(e, r) { var t = Object.keys(e); if (Object.getOwnPropertySymbols) { var o = Object.getOwnPropertySymbols(e); r && (o = o.filter(function (r) { return Object.getOwnPropertyDescriptor(e, r).enumerable; })), t.push.apply(t, o); } return t; }
function _objectSpread(e) { for (var r = 1; r < arguments.length; r++) { var t = null != arguments[r] ? arguments[r] : {}; r % 2 ? ownKeys(Object(t), true).forEach(function (r) { _defineProperty(e, r, t[r]); }) : Object.getOwnPropertyDescriptors ? Object.defineProperties(e, Object.getOwnPropertyDescriptors(t)) : ownKeys(Object(t)).forEach(function (r) { Object.defineProperty(e, r, Object.getOwnPropertyDescriptor(t, r)); }); } return e; }
function _defineProperty(obj, key, value) { key = _toPropertyKey(key); if (key in obj) { Object.defineProperty(obj, key, { value: value, enumerable: true, configurable: true, writable: true }); } else { obj[key] = value; } return obj; }
function _toPropertyKey(t) { var i = _toPrimitive(t, "string"); return "symbol" == typeof i ? i : i + ""; }
function _toPrimitive(t, r) { if ("object" != typeof t || !t) return t; var e = t[Symbol.toPrimitive]; if (void 0 !== e) { var i = e.call(t, r); if ("object" != typeof i) return i; throw new TypeError("@@toPrimitive must return a primitive value."); } return ("string" === r ? String : Number)(t); }
function Tree2Element(tree) {
  return tree && tree.map((node, i) => /*#__PURE__*/SP_REACT.createElement(node.tag, _objectSpread({
    key: i
  }, node.attr), Tree2Element(node.child)));
}
function GenIcon(data) {
  return props => /*#__PURE__*/SP_REACT.createElement(IconBase, _extends({
    attr: _objectSpread({}, data.attr)
  }, props), Tree2Element(data.child));
}
function IconBase(props) {
  var elem = conf => {
    var {
        attr,
        size,
        title
      } = props,
      svgProps = _objectWithoutProperties(props, _excluded);
    var computedSize = size || conf.size || "1em";
    var className;
    if (conf.className) className = conf.className;
    if (props.className) className = (className ? className + " " : "") + props.className;
    return /*#__PURE__*/SP_REACT.createElement("svg", _extends({
      stroke: "currentColor",
      fill: "currentColor",
      strokeWidth: "0"
    }, conf.attr, attr, svgProps, {
      className: className,
      style: _objectSpread(_objectSpread({
        color: props.color || conf.color
      }, conf.style), props.style),
      height: computedSize,
      width: computedSize,
      xmlns: "http://www.w3.org/2000/svg"
    }), title && /*#__PURE__*/SP_REACT.createElement("title", null, title), props.children);
  };
  return IconContext !== undefined ? /*#__PURE__*/SP_REACT.createElement(IconContext.Consumer, null, conf => elem(conf)) : elem(DefaultContext);
}

// THIS FILE IS AUTO GENERATED
function FaCheck (props) {
  return GenIcon({"attr":{"viewBox":"0 0 512 512"},"child":[{"tag":"path","attr":{"d":"M173.898 439.404l-166.4-166.4c-9.997-9.997-9.997-26.206 0-36.204l36.203-36.204c9.997-9.998 26.207-9.998 36.204 0L192 312.69 432.095 72.596c9.997-9.997 26.207-9.997 36.204 0l36.203 36.204c9.997 9.997 9.997 26.206 0 36.204l-294.4 294.401c-9.998 9.997-26.207 9.997-36.204-.001z"},"child":[]}]})(props);
}function FaDownload (props) {
  return GenIcon({"attr":{"viewBox":"0 0 512 512"},"child":[{"tag":"path","attr":{"d":"M216 0h80c13.3 0 24 10.7 24 24v168h87.7c17.8 0 26.7 21.5 14.1 34.1L269.7 378.3c-7.5 7.5-19.8 7.5-27.3 0L90.1 226.1c-12.6-12.6-3.7-34.1 14.1-34.1H192V24c0-13.3 10.7-24 24-24zm296 376v112c0 13.3-10.7 24-24 24H24c-13.3 0-24-10.7-24-24V376c0-13.3 10.7-24 24-24h146.7l49 49c20.1 20.1 52.5 20.1 72.6 0l49-49H488c13.3 0 24 10.7 24 24zm-124 88c0-11-9-20-20-20s-20 9-20 20 9 20 20 20 20-9 20-20zm64 0c0-11-9-20-20-20s-20 9-20 20 9 20 20 20 20-9 20-20z"},"child":[]}]})(props);
}function FaExclamationTriangle (props) {
  return GenIcon({"attr":{"viewBox":"0 0 576 512"},"child":[{"tag":"path","attr":{"d":"M569.517 440.013C587.975 472.007 564.806 512 527.94 512H48.054c-36.937 0-59.999-40.055-41.577-71.987L246.423 23.985c18.467-32.009 64.72-31.951 83.154 0l239.94 416.028zM288 354c-25.405 0-46 20.595-46 46s20.595 46 46 46 46-20.595 46-46-20.595-46-46-46zm-43.673-165.346l7.418 136c.347 6.364 5.609 11.346 11.982 11.346h48.546c6.373 0 11.635-4.982 11.982-11.346l7.418-136c.375-6.874-5.098-12.654-11.982-12.654h-63.383c-6.884 0-12.356 5.78-11.981 12.654z"},"child":[]}]})(props);
}function FaGamepad (props) {
  return GenIcon({"attr":{"viewBox":"0 0 640 512"},"child":[{"tag":"path","attr":{"d":"M480.07 96H160a160 160 0 1 0 114.24 272h91.52A160 160 0 1 0 480.07 96zM248 268a12 12 0 0 1-12 12h-52v52a12 12 0 0 1-12 12h-24a12 12 0 0 1-12-12v-52H84a12 12 0 0 1-12-12v-24a12 12 0 0 1 12-12h52v-52a12 12 0 0 1 12-12h24a12 12 0 0 1 12 12v52h52a12 12 0 0 1 12 12zm216 76a40 40 0 1 1 40-40 40 40 0 0 1-40 40zm64-96a40 40 0 1 1 40-40 40 40 0 0 1-40 40z"},"child":[]}]})(props);
}function FaSync (props) {
  return GenIcon({"attr":{"viewBox":"0 0 512 512"},"child":[{"tag":"path","attr":{"d":"M440.65 12.57l4 82.77A247.16 247.16 0 0 0 255.83 8C134.73 8 33.91 94.92 12.29 209.82A12 12 0 0 0 24.09 224h49.05a12 12 0 0 0 11.67-9.26 175.91 175.91 0 0 1 317-56.94l-101.46-4.86a12 12 0 0 0-12.57 12v47.41a12 12 0 0 0 12 12H500a12 12 0 0 0 12-12V12a12 12 0 0 0-12-12h-47.37a12 12 0 0 0-11.98 12.57zM255.83 432a175.61 175.61 0 0 1-146-77.8l101.8 4.87a12 12 0 0 0 12.57-12v-47.4a12 12 0 0 0-12-12H12a12 12 0 0 0-12 12V500a12 12 0 0 0 12 12h47.35a12 12 0 0 0 12-12.6l-4.15-82.57A247.17 247.17 0 0 0 255.83 504c121.11 0 221.93-86.92 243.55-201.82a12 12 0 0 0-11.8-14.18h-49.05a12 12 0 0 0-11.67 9.26A175.86 175.86 0 0 1 255.83 432z"},"child":[]}]})(props);
}function FaTimes (props) {
  return GenIcon({"attr":{"viewBox":"0 0 352 512"},"child":[{"tag":"path","attr":{"d":"M242.72 256l100.07-100.07c12.28-12.28 12.28-32.19 0-44.48l-22.24-22.24c-12.28-12.28-32.19-12.28-44.48 0L176 189.28 75.93 89.21c-12.28-12.28-32.19-12.28-44.48 0L9.21 111.45c-12.28 12.28-12.28 32.19 0 44.48L109.28 256 9.21 356.07c-12.28 12.28-12.28 32.19 0 44.48l22.24 22.24c12.28 12.28 32.2 12.28 44.48 0L176 322.72l100.07 100.07c12.28 12.28 32.2 12.28 44.48 0l22.24-22.24c12.28-12.28 12.28-32.19 0-44.48L242.72 256z"},"child":[]}]})(props);
}function FaTrash (props) {
  return GenIcon({"attr":{"viewBox":"0 0 448 512"},"child":[{"tag":"path","attr":{"d":"M432 32H312l-9.4-18.7A24 24 0 0 0 281.1 0H166.8a23.72 23.72 0 0 0-21.4 13.3L136 32H16A16 16 0 0 0 0 48v32a16 16 0 0 0 16 16h416a16 16 0 0 0 16-16V48a16 16 0 0 0-16-16zM53.2 467a48 48 0 0 0 47.9 45h245.8a48 48 0 0 0 47.9-45L416 128H32z"},"child":[]}]})(props);
}

/**
 * ProtonDB & Steam Deck Integration
 *
 * Two-step lookup for Epic/GOG games:
 * 1. Steam Store Search API (title → Steam AppID)
 * 2. ProtonDB API (appId → tier rating)
 *
 * Also fetches Steam Deck Verified status.
 */
// Memory cache for quick lookups (keyed by game title, normalized)
const compatCache = new Map();
const CACHE_TTL$1 = 24 * 60 * 60 * 1000; // 24 hours
// Memory cache for appId-based lookups (for Steam games)
const protonDBCache = new Map();
/**
 * Normalize title for cache key
 */
function normalizeTitle(title) {
    return title.toLowerCase().trim();
}
/**
 * Search Steam Store for a game by title, returns Steam AppID
 */
async function searchSteamStore(title) {
    try {
        const encoded = encodeURIComponent(title);
        const res = await fetchNoCors(`https://store.steampowered.com/api/storesearch/?term=${encoded}&cc=US`, { method: 'GET' });
        if (res.status === 200) {
            const data = await res.json();
            const items = data?.items;
            if (Array.isArray(items) && items.length > 0) {
                // Try to find best match by name similarity
                const normalizedSearch = normalizeTitle(title);
                const bestMatch = items.find((item) => normalizeTitle(item.name || '') === normalizedSearch) || items[0]; // Fall back to first result
                return {
                    appId: bestMatch.id,
                    name: bestMatch.name
                };
            }
        }
    }
    catch (error) {
        console.log('[Unifideck] Steam Store search error:', title, error);
    }
    return null;
}
/**
 * Fetch ProtonDB rating for a Steam AppID
 */
async function fetchProtonDBRating(appId) {
    try {
        const res = await fetchNoCors(`https://www.protondb.com/api/v1/reports/summaries/${appId}.json`, { method: 'GET' });
        if (res.status === 200) {
            const data = await res.json();
            return data?.tier || null;
        }
    }
    catch (error) {
        // 404 is normal for games not in ProtonDB
        if (!(error instanceof Error && error.message.includes('404'))) {
            console.log('[Unifideck] ProtonDB fetch error for app', appId, error);
        }
    }
    return null;
}
/**
 * Fetch Steam Deck verified status for a Steam AppID
 * Uses Steam's deck verification API
 */
async function fetchDeckVerifiedStatus(appId) {
    try {
        // Steam's deck compatibility API 
        const res = await fetchNoCors(`https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport?nAppID=${appId}`, { method: 'GET' });
        if (res.status === 200) {
            const data = await res.json();
            // Steam returns category: 1=unknown, 2=unsupported, 3=playable, 4=verified
            const category = data?.results?.resolved_category;
            switch (category) {
                case 4: return 'verified';
                case 3: return 'playable';
                case 2: return 'unsupported';
                default: return 'unknown';
            }
        }
    }
    catch (error) {
        // Silently fail - many games don't have deck data
    }
    return 'unknown';
}
/**
 * Get cached ProtonDB rating (synchronous - for filtering)
 */
function getCachedRating(appId) {
    const cached = protonDBCache.get(appId);
    if (cached && (Date.now() - cached.timestamp) < CACHE_TTL$1) {
        return cached.tier;
    }
    return null;
}
/**
 * Get full compatibility info for a game by title (for Epic/GOG games)
 * Two-step: Steam Store search → ProtonDB + Deck Verified
 */
async function getCompatByTitle(title) {
    const key = normalizeTitle(title);
    // Check cache
    const cached = compatCache.get(key);
    if (cached && (Date.now() - cached.timestamp) < CACHE_TTL$1) {
        return { tier: cached.tier, deckVerified: cached.deckVerified, steamAppId: cached.steamAppId };
    }
    // Step 1: Search Steam Store for AppID
    const searchResult = await searchSteamStore(title);
    if (!searchResult) {
        // Game not found on Steam
        const result = { tier: null, deckVerified: 'unknown', steamAppId: null };
        compatCache.set(key, { ...result, timestamp: Date.now() });
        return result;
    }
    const { appId } = searchResult;
    // Step 2: Fetch ProtonDB rating and Deck Verified status in parallel
    const [tier, deckVerified] = await Promise.all([
        fetchProtonDBRating(appId),
        fetchDeckVerifiedStatus(appId)
    ]);
    const result = {
        tier,
        deckVerified,
        steamAppId: appId
    };
    // Cache result
    compatCache.set(key, { ...result, timestamp: Date.now() });
    // Also cache in appId cache for future lookups
    if (tier) {
        protonDBCache.set(appId, { tier, timestamp: Date.now() });
    }
    console.log(`[Unifideck] Compat: "${title}" -> AppID ${appId}, tier=${tier}, deck=${deckVerified}`);
    return result;
}
/**
 * Get cached compatibility info by title (synchronous - for filtering)
 */
function getCachedCompatByTitle(title) {
    const key = normalizeTitle(title);
    const cached = compatCache.get(key);
    if (cached && (Date.now() - cached.timestamp) < CACHE_TTL$1) {
        return { tier: cached.tier, deckVerified: cached.deckVerified, steamAppId: cached.steamAppId };
    }
    return null;
}
/**
 * Pre-fetch compatibility info for a list of game titles
 * Runs in parallel with concurrency limit for speed
 */
async function prefetchCompatByTitles(titles) {
    console.log(`[Unifideck] Pre-fetching compatibility for ${titles.length} games...`);
    // Much faster: 10 concurrent lookups, minimal delay
    const batchSize = 10; // 10 concurrent API calls
    const delayMs = 50; // 200ms between batches (plenty for rate limiting)
    let processed = 0;
    let successful = 0;
    for (let i = 0; i < titles.length; i += batchSize) {
        const batch = titles.slice(i, i + batchSize);
        const results = await Promise.all(batch.map(title => getCompatByTitle(title)));
        processed += batch.length;
        successful += results.filter(r => r.tier !== null || r.deckVerified !== 'unknown').length;
        // Log progress every 50 games or at the end
        if (processed % 50 === 0 || processed === titles.length) {
            console.log(`[Unifideck] Compat prefetch: ${processed}/${titles.length} (${successful} found)`);
        }
        // Small delay between batches to be nice to APIs
        if (i + batchSize < titles.length) {
            await new Promise(resolve => setTimeout(resolve, delayMs));
        }
    }
    console.log(`[Unifideck] Compat prefetch complete: ${titles.length} games, ${successful} with ratings`);
}
/**
 * Check if a rating meets the "Great on Deck" criteria
 */
function meetsGreatOnDeckCriteria(compat) {
    if (!compat)
        return false;
    // Steam Deck Verified always passes
    if (compat.deckVerified === 'verified')
        return true;
    // ProtonDB Native or Platinum passes (regardless of Deck status)
    if (compat.tier === 'native' || compat.tier === 'platinum')
        return true;
    // Gold only passes if ALSO Steam Deck Verified (already handled above)
    // So Gold-only games without Deck Verified will fail here
    return false;
}

/**
 * Unifideck Tab Filters
 *
 * Filters for creating custom library tabs that include
 * Steam, Epic, GOG, and Amazon games with proper store detection.
 */
// Non-Steam shortcut app_type value
const NON_STEAM_APP_TYPE = 1073741824;
// Steam Deck compatibility categories
const DECK_VERIFIED = 3; // steam_deck_compat_category
// Cache for Unifideck game info (store mapping, install status, and Steam appId for ProtonDB)
// Key is appId - we store BOTH signed and unsigned versions for lookup
const unifideckGameCache = new Map();
/**
 * Updates the Unifideck game cache with store info
 * Stores both signed and unsigned versions of appId for reliable lookup
 */
function updateUnifideckCache(games) {
    console.log(`[Unifideck] Updating game cache with ${games.length} games`);
    unifideckGameCache.clear();
    games.forEach(game => {
        const signedId = game.appId;
        // Convert signed to unsigned and vice versa for reliable lookup
        const unsignedId = signedId < 0 ? signedId + 0x100000000 : signedId;
        const altSignedId = signedId >= 0 && signedId > 0x7FFFFFFF ? signedId - 0x100000000 : signedId;
        const entry = {
            store: game.store,
            isInstalled: game.isInstalled,
            steamAppId: game.steamAppId
        };
        unifideckGameCache.set(signedId, entry);
        unifideckGameCache.set(unsignedId, entry);
        if (altSignedId !== signedId) {
            unifideckGameCache.set(altSignedId, entry);
        }
    });
    console.log(`[Unifideck] Cache now has ${unifideckGameCache.size} entries (${games.length} games x2 for signed/unsigned)`);
}
/**
 * Check if a game is a Unifideck-managed game
 */
function isUnifideckGame(appId) {
    return unifideckGameCache.has(appId);
}
/**
 * Gets the store for a given app
 * Returns null if unknown non-Steam shortcut (not in our cache)
 */
function getStoreForApp(appId, appType) {
    // Check cache first (works for both signed and unsigned appId)
    const cached = unifideckGameCache.get(appId);
    if (cached) {
        return cached.store;
    }
    // If it's a non-Steam shortcut but not in our cache, return null (unknown)
    // This lets us filter it out from Epic/GOG/Steam tabs but keep in Non-Steam
    if (appType === NON_STEAM_APP_TYPE) {
        return null; // Unknown non-Steam shortcut
    }
    return 'steam'; // Native Steam game
}
/**
 * Gets installed status for a game from our cache
 */
function getInstalledStatus(appId, appType, steamInstalledFlag) {
    // For Unifideck games, use our cache
    const cached = unifideckGameCache.get(appId);
    if (cached) {
        return cached.isInstalled;
    }
    // For Steam games, use Steam's installed flag
    return steamInstalledFlag;
}
/**
 * Filter functions for each filter type
 */
const filterFunctions = {
    // Show all Steam games AND Unifideck games (but not unrelated non-Steam shortcuts)
    all: (_params, app) => {
        // Native Steam games - always include
        if (app.app_type !== NON_STEAM_APP_TYPE) {
            return true;
        }
        // Non-Steam shortcuts - only include if it's a Unifideck game
        return isUnifideckGame(app.appid);
    },
    // Filter by installation status
    // Shows ALL installed games/shortcuts (Steam, Unifideck, and other non-Steam)
    installed: (params, app) => {
        const isInstalled = getInstalledStatus(app.appid, app.app_type, app.installed);
        return params.installed ? isInstalled : !isInstalled;
    },
    // Filter by platform (Steam vs non-Steam)
    platform: (params, app) => {
        if (params.platform === 'all')
            return true;
        if (params.platform === 'steam') {
            return app.app_type !== NON_STEAM_APP_TYPE;
        }
        return app.app_type === NON_STEAM_APP_TYPE;
    },
    // Filter by store (Steam, Epic, GOG, Amazon)
    store: (params, app) => {
        if (params.store === 'all')
            return true;
        const store = getStoreForApp(app.appid, app.app_type);
        // If store is null (unknown non-Steam shortcut), don't include in any store tab
        if (store === null) {
            return false;
        }
        return store === params.store;
    },
    // Filter by Steam Deck compatibility
    // Includes: Native, Platinum, or Steam Deck Verified
    deckCompat: (_params, app) => {
        // Steam Deck Verified - always pass (Steam games only)
        if (app.steam_deck_compat_category === DECK_VERIFIED) {
            return true;
        }
        // For Unifideck games (Epic/GOG/Amazon), use title-based compatibility lookup
        const cached = unifideckGameCache.get(app.appid);
        if (cached) {
            // Use app's display_name for title-based search
            const title = app.display_name || '';
            if (title) {
                const compat = getCachedCompatByTitle(title);
                return meetsGreatOnDeckCriteria(compat);
            }
            return false;
        }
        // For Steam games, use appId-based ProtonDB lookup
        const protonRating = getCachedRating(app.appid);
        if (protonRating) {
            if (protonRating === 'native' || protonRating === 'platinum') {
                return true;
            }
        }
        // No rating or doesn't meet criteria
        return false;
    },
    // Non-Steam tab: All non-Steam shortcuts EXCEPT non-installed Unifideck games
    nonSteam: (_params, app) => {
        // Only include non-Steam shortcuts
        if (app.app_type !== NON_STEAM_APP_TYPE) {
            return false;
        }
        // Check if it's a Unifideck game
        const cached = unifideckGameCache.get(app.appid);
        if (cached) {
            // It's a Unifideck game - only show if installed
            return cached.isInstalled;
        }
        // Not a Unifideck game - show all other non-Steam shortcuts
        return true;
    }
};
/**
 * Runs a filter against an app
 */
function runFilter(filter, app) {
    const filterFn = filterFunctions[filter.type];
    if (!filterFn)
        return true;
    return filterFn(filter.params, app);
}
/**
 * Runs multiple filters against an app (AND logic)
 */
function runFilters(filters, app) {
    return filters.every(filter => runFilter(filter, app));
}

/**
 * Unifideck Tab Container
 *
 * Manages custom tabs for the Steam library that include
 * Epic, GOG, and Amazon games alongside Steam games.
 */

// Default Unifideck tabs - ORDERED: Great on Deck, All Games, Installed, Steam, Epic, GOG, Amazon, Non-Steam
const UNIFIDECK_TABS = [
    {
        id: 'unifideck-deck',
        title: 'Great on Deck',
        position: 0,
        filters: [{ type: 'deckCompat', params: {} }] // Native, Platinum, or Verified only
    },
    {
        id: 'unifideck-all',
        title: 'All Games', // Renamed from "All"
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
        filters: [{ type: 'nonSteam', params: {} }] // All non-Steam shortcuts except non-installed Unifideck
    }
];
// IDs of default Steam tabs to hide (when TabMaster is NOT present)
// Note: Non-Steam tab is called 'DesktopApps' internally by Steam!
const DEFAULT_TABS_TO_HIDE = [
    'GreatOnDeck',
    'AllGames',
    'Installed',
    'DesktopApps', // This is Steam's actual ID for the Non-Steam tab!
];
/**
 * Check if TabMaster plugin is installed
 */
function isTabMasterInstalled() {
    try {
        const plugins = window.DeckyPluginLoader?.plugins ?? [];
        return plugins.some((p) => p.name === 'TabMaster' || p.name === 'Tab Master');
    }
    catch {
        return false;
    }
}
/**
 * Get tabs to hide based on TabMaster presence
 * If TabMaster is present, we don't hide any tabs (user can manage via TabMaster)
 */
function getHiddenDefaultTabs() {
    if (isTabMasterInstalled()) {
        console.log('[Unifideck] TabMaster detected - not hiding default tabs');
        return []; // Don't hide any tabs, let TabMaster manage
    }
    return DEFAULT_TABS_TO_HIDE;
}
/**
 * Custom Tab Container for Unifideck
 * Builds a filtered collection of games for each tab
 */
class UnifideckTabContainer {
    constructor(tab) {
        this.id = tab.id;
        this.title = tab.title;
        this.position = tab.position;
        this.filters = tab.filters;
        // Initialize collection structure
        this.collection = {
            AsDeletableCollection: () => null,
            AsDragDropCollection: () => null,
            AsEditableCollection: () => null,
            GetAppCountWithToolsFilter: (appFilter) => this.collection.visibleApps.filter((app) => appFilter.Matches(app)).length,
            bAllowsDragAndDrop: false,
            bIsDeletable: false,
            bIsDynamic: false,
            bIsEditable: false,
            displayName: this.title,
            id: this.id,
            allApps: [],
            visibleApps: [],
            apps: new Map()
        };
        this.buildCollection();
    }
    /**
     * Builds the filtered collection of apps for this tab
     */
    buildCollection() {
        try {
            // Get all games from Steam's collection store
            const allGamesCollection = window.collectionStore?.GetCollection('type-games');
            if (!allGamesCollection) {
                console.log('[Unifideck] Could not access collectionStore');
                return;
            }
            const allApps = allGamesCollection.allApps || [];
            // Filter apps based on tab filters
            const filteredApps = allApps.filter((app) => runFilters(this.filters, app));
            this.collection.allApps = filteredApps;
            this.collection.visibleApps = [...filteredApps];
            // Build apps map
            const appMap = new Map();
            filteredApps.forEach((app) => {
                appMap.set(app.appid, app);
            });
            this.collection.apps = appMap;
            console.log(`[Unifideck] Tab "${this.title}" has ${filteredApps.length} games`);
        }
        catch (error) {
            console.error('[Unifideck] Error building collection:', error);
        }
    }
    /**
     * Gets the SteamTab object for injection into the library
     */
    getActualTab(TabAppGrid, TabContext, sortingProps, collectionAppFilter) {
        // Rebuild collection to ensure fresh data
        this.buildCollection();
        // Create the tab content
        const createContent = (inner) => TabContext
            ? SP_REACT.createElement(TabContext.Provider, { value: { label: this.title } }, inner)
            : inner;
        return {
            title: this.title,
            id: this.id,
            footer: {},
            content: createContent(SP_REACT.createElement(TabAppGrid, {
                collection: this.collection,
                setSortBy: sortingProps.setSortBy,
                eSortBy: sortingProps.eSortBy,
                showSortingContextMenu: sortingProps.showSortingContextMenu
            })),
            renderTabAddon: () => {
                return SP_REACT.createElement('span', { className: DFL.gamepadTabbedPageClasses?.TabCount || '' }, this.collection.GetAppCountWithToolsFilter(collectionAppFilter));
            }
        };
    }
}
// Tab manager singleton
class TabManager {
    constructor() {
        this.tabs = [];
        this.initialized = false;
        this.cacheLoaded = false;
        this.epicGameCount = 0;
        this.gogGameCount = 0;
        this.amazonGameCount = 0;
    }
    async initialize() {
        if (this.initialized)
            return;
        // Load game cache from backend
        await this.loadGameCache();
        this.tabs = UNIFIDECK_TABS.map(tab => new UnifideckTabContainer(tab));
        this.initialized = true;
        console.log('[Unifideck] TabManager initialized with', this.tabs.length, 'tabs');
    }
    /**
     * Load game cache from backend
     */
    async loadGameCache() {
        if (this.cacheLoaded)
            return;
        try {
            console.log('[Unifideck] Loading game cache from backend...');
            const games = await call('get_all_unifideck_games');
            if (Array.isArray(games) && games.length > 0) {
                const cacheData = games.map(g => ({
                    appId: g.appId,
                    store: g.store,
                    isInstalled: g.isInstalled
                }));
                updateUnifideckCache(cacheData);
                // Count games by store for tab visibility
                this.epicGameCount = games.filter((g) => g.store === 'epic').length;
                this.gogGameCount = games.filter((g) => g.store === 'gog').length;
                this.amazonGameCount = games.filter((g) => g.store === 'amazon').length;
                console.log(`[Unifideck] Loaded ${games.length} games into cache (Epic: ${this.epicGameCount}, GOG: ${this.gogGameCount}, Amazon: ${this.amazonGameCount})`);
                // Prefetch compatibility info (ProtonDB + Deck Verified) for Epic/GOG/Amazon games
                const titles = games
                    .filter((g) => g.title)
                    .map((g) => g.title);
                if (titles.length > 0) {
                    console.log(`[Unifideck] Prefetching compatibility for ${titles.length} games...`);
                    // Run in background - don't block tab initialization
                    prefetchCompatByTitles(titles).catch((err) => console.error('[Unifideck] Compat prefetch error:', err));
                }
                this.cacheLoaded = true;
            }
            else {
                console.log('[Unifideck] No Unifideck games found in backend');
            }
        }
        catch (error) {
            console.error('[Unifideck] Error loading game cache:', error);
        }
    }
    getTabs() {
        return this.tabs.filter(tab => this.shouldShowTab(tab.id));
    }
    /**
     * Determines if a tab should be visible based on game availability
     */
    shouldShowTab(tabId) {
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
    isInitialized() {
        return this.initialized;
    }
    rebuildTabs() {
        this.tabs.forEach(tab => tab.buildCollection());
    }
    /**
     * Updates the game cache with Unifideck game info
     */
    updateGameCache(games) {
        updateUnifideckCache(games);
        this.cacheLoaded = true;
        this.rebuildTabs();
    }
}
const tabManager = new TabManager();

/**
 * Unifideck Library Patch
 *
 * Patches the Steam library to inject custom tabs that include
 * Epic, GOG, and Amazon games alongside Steam games.
 *
 * When TabMaster is detected, custom tabs are NOT injected - instead,
 * users can use [Unifideck] collections via TabMaster.
 */

// Cache for tab app grid component
let TabAppGridComponent = undefined;
/**
 * Adds a route patch, removing any existing patches first
 */
function addPatch(route, patch) {
    // Remove any existing patches to prevent duplicates
    try {
        const existingPatches = [...(window.DeckyPluginLoader?.routerHook?.routerState?._routePatches?.get(route) ?? [])];
        existingPatches.forEach(existingPatch => {
            if (patch.toString() === existingPatch.toString()) {
                routerHook.removePatch(route, existingPatch);
            }
        });
    }
    catch (e) {
        // Ignore errors during cleanup
    }
    return routerHook.addPatch(route, patch);
}
/**
 * Patches the Steam library to show Unifideck tabs
 */
function patchLibrary() {
    // Initialize tab manager asynchronously
    tabManager.initialize().catch(err => console.error('[Unifideck] TabManager init error:', err));
    return addPatch('/library', (props) => {
        // Check if TabMaster is installed
        if (isTabMasterInstalled()) {
            console.log('[Unifideck] TabMaster detected - skipping custom tab injection (use [Unifideck] collections instead)');
            // Don't inject tabs, let Steam + TabMaster handle it
            return props;
        }
        DFL.afterPatch(props.children, 'type', (_, ret1) => {
            if (!ret1?.type) {
                console.error('[Unifideck] Failed to find outer library element');
                return ret1;
            }
            SP_REACT.useState(false);
            let innerPatch;
            let memoCache;
            SP_REACT.useEffect(() => {
                // Cleanup on unmount
                return () => {
                    if (innerPatch)
                        innerPatch.unpatch();
                };
            });
            // Patch the inner library component
            DFL.afterPatch(ret1, 'type', (_, ret2) => {
                if (!ret2?.type) {
                    console.error('[Unifideck] Failed to find inner library element');
                    return ret2;
                }
                if (memoCache) {
                    ret2.type = memoCache;
                }
                else {
                    // @ts-ignore
                    const origMemoComponent = ret2.type.type;
                    // @ts-ignore
                    DFL.wrapReactType(ret2);
                    // Replace the component's type function
                    innerPatch = DFL.replacePatch(ret2.type, 'type', (args) => {
                        // Get React hooks from internal structure
                        const hooks = window.SP_REACT?.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED?.ReactCurrentDispatcher?.current ||
                            Object.values(window.SP_REACT?.__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE || {}).find((p) => p?.useEffect);
                        if (!hooks?.useMemo) {
                            return origMemoComponent(...args);
                        }
                        const realUseMemo = hooks.useMemo;
                        // Fake useMemo to intercept tab creation
                        const fakeUseMemo = (fn, deps) => {
                            return realUseMemo(() => {
                                const tabs = fn();
                                // Only intercept if we got an array of tabs
                                if (!Array.isArray(tabs)) {
                                    return tabs;
                                }
                                // Check if TabManager is initialized
                                if (!tabManager.isInitialized()) {
                                    console.log('[Unifideck] TabManager not initialized, showing default tabs');
                                    return tabs;
                                }
                                // Extract sorting props from deps
                                const [eSortBy, setSortBy, showSortingContextMenu] = deps;
                                const sortingProps = { eSortBy, setSortBy, showSortingContextMenu };
                                const collectionsAppFilterGamepad = deps[6];
                                // Find a template tab to copy component structure from
                                const tabTemplate = tabs.find((tab) => tab?.id === 'AllGames');
                                if (!tabTemplate) {
                                    console.warn('[Unifideck] Could not find AllGames template tab');
                                    return tabs;
                                }
                                // Find the TabAppGrid component
                                const TabAppGrid = TabAppGridComponent ??
                                    DFL.findInReactTree(tabTemplate.content, (elt) => elt?.type?.toString?.().includes('Library_FilteredByHeader'))?.type;
                                if (!TabAppGrid) {
                                    console.warn('[Unifideck] Could not find TabAppGrid component');
                                    return tabs;
                                }
                                TabAppGridComponent = TabAppGrid;
                                // Get TabContext for proper labeling
                                const TabContext = tabTemplate.content.type?._context;
                                // Build Unifideck tabs
                                const unifideckTabs = tabManager.getTabs();
                                const customTabs = unifideckTabs
                                    .map(tabContainer => tabContainer.getActualTab(TabAppGrid, TabContext, sortingProps, collectionsAppFilterGamepad))
                                    .filter((tab) => tab !== null);
                                // Filter out default tabs that we're replacing
                                const hiddenTabs = getHiddenDefaultTabs();
                                const filteredDefaultTabs = tabs.filter(tab => !hiddenTabs.includes(tab.id));
                                // Return custom tabs first, then remaining default tabs
                                console.log(`[Unifideck] Showing ${customTabs.length} custom tabs + ${filteredDefaultTabs.length} default tabs (hidden: ${hiddenTabs.length})`);
                                return [...customTabs, ...filteredDefaultTabs];
                            }, deps);
                        };
                        // Temporarily replace useMemo
                        hooks.useMemo = fakeUseMemo;
                        const res = origMemoComponent(...args);
                        hooks.useMemo = realUseMemo;
                        return res;
                    });
                    memoCache = ret2.type;
                }
                return ret2;
            });
            return ret1;
        });
        return props;
    });
}

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
// Prefix for all Unifideck collections
const COLLECTION_PREFIX = '[Unifideck] ';
/**
 * Get collectionStore from window
 */
function getCollectionStore() {
    return window.collectionStore ?? null;
}
/**
 * Get appStore for app overviews
 */
function getAppStoreEx() {
    const appStore = window.appStore;
    if (!appStore)
        return null;
    return {
        getAppOverview: (appId) => {
            try {
                return appStore.GetAppOverviewByAppID(appId) ?? null;
            }
            catch {
                return null;
            }
        }
    };
}
/**
 * Get all apps from Steam's collection
 */
function getAllApps() {
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
    }
    catch (e) {
        console.error('[Unifideck Collections] Error getting type-games:', e);
        return [];
    }
}
/**
 * Convert tab title to collection name
 */
function tabToCollectionName(tab) {
    return `${COLLECTION_PREFIX}${tab.title}`;
}
/**
 * Get all valid Unifideck collection names (from current UNIFIDECK_TABS)
 */
function getValidCollectionNames() {
    return new Set(UNIFIDECK_TABS.map(tabToCollectionName));
}
/**
 * Delete a collection by ID
 */
async function deleteCollection(collection) {
    try {
        await collection.Delete();
        console.log(`[Unifideck Collections] Deleted stale collection: ${collection.displayName}`);
    }
    catch (e) {
        console.error(`[Unifideck Collections] Failed to delete ${collection.displayName}:`, e);
    }
}
/**
 * Clean up old/stale Unifideck collections that don't match current tabs
 */
async function cleanupStaleCollections() {
    const collectionStore = getCollectionStore();
    if (!collectionStore)
        return;
    // Safety check: userCollections may not exist on all Steam versions
    const userCollections = collectionStore.userCollections;
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
async function getOrCreateCollection(tag) {
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
async function clearCollection(collection) {
    const existingApps = collection.allApps ?? [];
    if (existingApps.length > 0) {
        collection.AsDragDropCollection().RemoveApps(existingApps);
        await collection.Save();
    }
}
/**
 * Sync a single tab's apps to its corresponding collection
 */
async function syncTabToCollection(tab, allApps) {
    const collectionName = tabToCollectionName(tab);
    // Filter apps using the SAME logic as custom tabs
    const matchingApps = allApps.filter(app => {
        if (app.appid <= 0)
            return false;
        return runFilters(tab.filters, app);
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
    const overviews = [];
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
async function syncUnifideckCollections() {
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
    const results = await Promise.allSettled(UNIFIDECK_TABS.map(tab => syncTabToCollection(tab, allApps)));
    const succeeded = results.filter(r => r.status === 'fulfilled' && r.value).length;
    console.log(`[Unifideck Collections] ✓ Sync complete: ${succeeded}/${UNIFIDECK_TABS.length} collections updated`);
}

/**
 * Format bytes to human-readable size
 */
function formatBytes(bytes) {
    if (bytes === 0)
        return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
}
/**
 * Format seconds to HH:MM:SS
 */
function formatETA(seconds) {
    if (seconds <= 0)
        return "--:--";
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    if (hrs > 0) {
        return `${hrs}:${mins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
    }
    return `${mins}:${secs.toString().padStart(2, "0")}`;
}
/**
 * Store icon based on store type
 */
const StoreIcon = ({ store }) => {
    const color = store === "epic" ? "#0078f2" : store === "amazon" ? "#FF9900" : "#a855f7";
    return (SP_JSX.jsx("span", { style: {
            display: "inline-block",
            width: "8px",
            height: "8px",
            borderRadius: "50%",
            backgroundColor: color,
            marginRight: "8px",
        } }));
};
/**
 * Single download item display
 */
const DownloadItemRow = ({ item, isCurrent, onCancel, onClear }) => {
    const statusColors = {
        downloading: "#1a9fff",
        queued: "#888",
        completed: "#4ade80",
        cancelled: "#f59e0b",
        error: "#ef4444",
    };
    return (SP_JSX.jsxs("div", { style: {
            backgroundColor: "#1e2329",
            borderRadius: "8px",
            padding: "12px",
            marginBottom: "8px",
            border: isCurrent ? "1px solid #1a9fff" : "1px solid #333",
        }, children: [SP_JSX.jsxs("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" }, children: [SP_JSX.jsxs("div", { style: { display: "flex", alignItems: "center", flex: 1 }, children: [SP_JSX.jsx(StoreIcon, { store: item.store }), SP_JSX.jsx("span", { style: { fontWeight: "bold", color: "#fff", fontSize: "14px" }, children: item.game_title })] }), (item.status === "completed" || item.status === "error" || item.status === "cancelled") && onClear && (SP_JSX.jsx(DFL.DialogButton, { onClick: () => onClear(item.id), style: {
                            padding: "0",
                            width: "20px",
                            height: "20px",
                            minWidth: "auto",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            backgroundColor: "transparent",
                            color: "#666",
                        }, children: SP_JSX.jsx(FaTimes, { size: 10 }) }))] }), (item.status === "downloading" || item.status === "queued") && (SP_JSX.jsx("div", { style: { marginBottom: "8px" }, children: SP_JSX.jsxs(DFL.DialogButton, { onClick: () => onCancel(item.id), style: {
                        padding: "4px 12px",
                        minWidth: "auto",
                        backgroundColor: "rgba(239, 68, 68, 0.2)",
                        color: "#ef4444",
                        fontSize: "12px",
                    }, children: [SP_JSX.jsx(FaTimes, { size: 10, style: { marginRight: "4px" } }), " Cancel"] }) })), item.status === "downloading" && (SP_JSX.jsx(SP_JSX.Fragment, { children: item.progress_percent === 0 && item.downloaded_bytes === 0 ? (SP_JSX.jsx("div", { style: { fontSize: "12px", color: "#888", fontStyle: "italic" }, children: "Preparing download..." })) : (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx("div", { style: {
                                width: "100%",
                                height: "6px",
                                backgroundColor: "#333",
                                borderRadius: "3px",
                                overflow: "hidden",
                                marginBottom: "8px",
                            }, children: SP_JSX.jsx("div", { style: {
                                    width: `${item.progress_percent}%`,
                                    height: "100%",
                                    backgroundColor: "#1a9fff",
                                    transition: "width 0.3s ease",
                                } }) }), SP_JSX.jsxs("div", { style: { display: "flex", justifyContent: "space-between", fontSize: "12px", color: "#888" }, children: [SP_JSX.jsxs("span", { children: [item.progress_percent.toFixed(1), "%"] }), SP_JSX.jsxs("span", { children: [formatBytes(item.downloaded_bytes), " / ", formatBytes(item.total_bytes)] }), SP_JSX.jsxs("span", { children: [item.speed_mbps.toFixed(1), " MB/s"] }), SP_JSX.jsxs("span", { children: ["ETA: ", formatETA(item.eta_seconds)] })] })] })) })), item.status !== "downloading" && (SP_JSX.jsxs("div", { style: { display: "flex", alignItems: "center", fontSize: "12px", color: statusColors[item.status] }, children: [item.status === "queued" && SP_JSX.jsx(FaDownload, { size: 10, style: { marginRight: "4px" } }), item.status === "completed" && SP_JSX.jsx(FaCheck, { size: 10, style: { marginRight: "4px" } }), item.status === "error" && SP_JSX.jsx(FaExclamationTriangle, { size: 10, style: { marginRight: "4px" } }), SP_JSX.jsx("span", { style: { textTransform: "capitalize" }, children: item.status }), item.error_message && (SP_JSX.jsxs("span", { style: { marginLeft: "8px", color: "#888" }, children: ["- ", item.error_message] }))] }))] }));
};
/**
 * Empty state display
 */
const EmptyState = ({ message }) => (SP_JSX.jsx("div", { style: {
        textAlign: "center",
        padding: "20px",
        color: "#888",
        fontSize: "14px",
    }, children: message }));
/**
 * Main Downloads Tab Component
 */
const DownloadsTab = () => {
    const [queueInfo, setQueueInfo] = SP_REACT.useState(null);
    const [loading, setLoading] = SP_REACT.useState(true);
    const pollIntervalRef = SP_REACT.useRef(null);
    // Fetch queue info
    const fetchQueueInfo = async () => {
        try {
            const result = await call("get_download_queue_info");
            if (result.success) {
                setQueueInfo(result);
            }
        }
        catch (error) {
            console.error("[DownloadsTab] Error fetching queue info:", error);
        }
        setLoading(false);
    };
    // Start polling when component mounts
    SP_REACT.useEffect(() => {
        fetchQueueInfo();
        // Poll every second for progress updates
        pollIntervalRef.current = setInterval(fetchQueueInfo, 1000);
        return () => {
            if (pollIntervalRef.current) {
                clearInterval(pollIntervalRef.current);
            }
        };
    }, []);
    // Handle cancel
    const handleCancel = async (downloadId) => {
        try {
            const result = await call("cancel_download_by_id", downloadId);
            if (result.success) {
                toaster.toast({
                    title: "Download Cancelled",
                    body: "The download has been removed from the queue.",
                    duration: 3000,
                });
                fetchQueueInfo(); // Refresh immediately
            }
            else {
                toaster.toast({
                    title: "Cancel Failed",
                    body: result.error || "Unknown error",
                    duration: 5000,
                    critical: true,
                });
            }
        }
        catch (error) {
            console.error("[DownloadsTab] Error cancelling download:", error);
        }
    };
    // Handle clear finished item
    const handleClear = async (downloadId) => {
        try {
            const result = await call("clear_finished_download", downloadId);
            if (result.success) {
                fetchQueueInfo(); // Refresh to remove the item
            }
        }
        catch (error) {
            console.error("[DownloadsTab] Error clearing finished download:", error);
        }
    };
    if (loading) {
        return (SP_JSX.jsx(DFL.PanelSection, { title: "DOWNLOADS", children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.Field, { label: "Loading...", children: SP_JSX.jsx("span", { style: { color: "#888" }, children: "Fetching download queue..." }) }) }) }));
    }
    const current = queueInfo?.current;
    const queued = queueInfo?.queued || [];
    const finished = queueInfo?.finished || [];
    const hasActiveDownloads = current || queued.length > 0;
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSection, { title: "CURRENT DOWNLOAD", children: current ? (SP_JSX.jsx(DownloadItemRow, { item: current, isCurrent: true, onCancel: handleCancel })) : (SP_JSX.jsx(EmptyState, { message: "No active downloads" })) }), queued.length > 0 && (SP_JSX.jsx(DFL.PanelSection, { title: `QUEUED (${queued.length})`, children: queued.map((item) => (SP_JSX.jsx(DownloadItemRow, { item: item, isCurrent: false, onCancel: handleCancel }, item.id))) })), finished.length > 0 && (SP_JSX.jsx(DFL.PanelSection, { title: "RECENTLY COMPLETED", children: finished.slice(0, 5).map((item) => (SP_JSX.jsx(DownloadItemRow, { item: item, isCurrent: false, onCancel: () => { }, onClear: handleClear }, item.id))) })), !hasActiveDownloads && finished.length === 0 && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(EmptyState, { message: "No downloads. Install games from your library to see them here." }) }))] }));
};

/**
 * Storage Location Settings Component
 */
const StorageSettings = () => {
    const [locations, setLocations] = SP_REACT.useState([]);
    const [defaultStorage, setDefaultStorage] = SP_REACT.useState("internal");
    const [saving, setSaving] = SP_REACT.useState(false);
    // Fetch storage locations on mount
    SP_REACT.useEffect(() => {
        const fetchLocations = async () => {
            try {
                const result = await call("get_storage_locations");
                if (result.success) {
                    setLocations(result.locations);
                    setDefaultStorage(result.default);
                }
            }
            catch (error) {
                console.error("[StorageSettings] Error fetching locations:", error);
            }
        };
        fetchLocations();
    }, []);
    // Handle storage location change
    const handleStorageChange = async (option) => {
        const newLocation = option.data;
        setSaving(true);
        try {
            const result = await call("set_default_storage_location", newLocation);
            if (result.success) {
                setDefaultStorage(newLocation);
                toaster.toast({
                    title: "Storage Location Updated",
                    body: `New games will be installed to ${option.label}`,
                    duration: 3000,
                });
            }
            else {
                toaster.toast({
                    title: "Failed to Update",
                    body: result.error || "Unknown error",
                    duration: 5000,
                    critical: true,
                });
            }
        }
        catch (error) {
            console.error("[StorageSettings] Error setting storage location:", error);
        }
        setSaving(false);
    };
    // Build dropdown options
    const dropdownOptions = locations
        .filter((loc) => loc.available)
        .map((loc) => ({
        data: loc.id,
        label: `${loc.label} (${loc.free_space_gb} GB free)`,
    }));
    const selectedOption = dropdownOptions.find((opt) => opt.data === defaultStorage);
    return (SP_JSX.jsxs(DFL.PanelSection, { title: "DOWNLOAD SETTINGS", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.Field, { label: "Install Location", description: "Where new games will be downloaded", children: dropdownOptions.length > 0 ? (SP_JSX.jsx(DFL.Dropdown, { rgOptions: dropdownOptions, selectedOption: selectedOption?.data, onChange: handleStorageChange, disabled: saving })) : (SP_JSX.jsx("span", { style: { color: "#888", fontSize: "12px" }, children: "Loading storage options..." })) }) }), locations.length > 0 && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.Field, { label: "Path", children: SP_JSX.jsx("span", { style: { color: "#888", fontSize: "12px" }, children: locations.find((l) => l.id === defaultStorage)?.path || "Unknown" }) }) }))] }));
};

// ========== INSTALL BUTTON FEATURE ==========
//
// CRITICAL: Use routerHook.addPatch + React.createElement ONLY
// Vanilla DOM manipulation does NOT work due to CEF process isolation.
//
// WHY THIS PATTERN IS REQUIRED:
// - Steam Deck UI runs in Chromium Embedded Framework (CEF)
// - Decky plugins execute in separate CEF process (about:blank?createflags=...)
// - Steam UI renders in different process (steamloopback.host)
// - DOM elements cannot be directly injected across process boundaries
//
// WHAT DOESN'T WORK (tried in v52-v68):
// - document.createElement() + appendChild() - creates elements in wrong process
// - ReactDOM.createPortal() - portal target not accessible
// - Direct DOM manipulation - Steam's React overwrites changes
//
// WHAT WORKS (ProtonDB/HLTB pattern):
// - routerHook.addPatch() intercepts React route rendering IN Steam's process
// - React.createElement() creates components in Steam's React tree
// - Steam's reconciler renders these in its own DOM
// - ✅ This is the ONLY way to inject UI into Steam's game details page
//
// ARCHITECTURE:
// - GameDetailsWithInstallButton: Wrapper component with React hooks for state management
// - InstallButtonComponent: Button UI with loading states (shows in game header)
// - InstallOverlayComponent: Modal overlay (click-triggered, not auto-show)
//
// STATE FLOW:
// 1. User navigates to game details → GameDetailsWithInstallButton mounts
// 2. useEffect fetches game info (async) → Shows "Checking..." button
// 3. Game info loaded → Shows "Install [Game]" button
// 4. User clicks Install button → showOverlay = true
// 5. Overlay shows → User clicks "Install Now"
// 6. Installation runs → onInstallComplete() → Toast notification
// 7. Button updates → "Restart Steam to Play" message
// 8. User restarts Steam → Shortcut updated and functional
//
// KEY FIX (v70):
// - Component-level state prevents async/sync race conditions
// - useEffect with [appId] dependency ensures state resets per-game
// - 30-second cache reduces redundant backend calls
//
// ================================================
// Global cache for game info (5-second TTL for faster updates after installation)
const gameInfoCache = new Map();
const CACHE_TTL = 5000; // 5 seconds - reduced from 30s for faster button state updates
// ========== END INSTALL BUTTON FEATURE ==========
// ========== NATIVE PLAY BUTTON OVERRIDE ==========
// 
// This component shows alongside the native Play button for uninstalled Unifideck games.
// For installed games, we hide this and let Steam's native Play button work.
// For uninstalled games, we show an Install button with size info.
//
// ================================================
// Install Info Display Component - shows download size next to play section
const InstallInfoDisplay = ({ appId }) => {
    const [gameInfo, setGameInfo] = SP_REACT.useState(null);
    const [processing, setProcessing] = SP_REACT.useState(false);
    const [downloadState, setDownloadState] = SP_REACT.useState({ isDownloading: false });
    const pollIntervalRef = SP_REACT.useRef(null);
    // Fetch game info on mount
    SP_REACT.useEffect(() => {
        const cached = gameInfoCache.get(appId);
        if (cached && (Date.now() - cached.timestamp) < CACHE_TTL) {
            setGameInfo(cached.info);
            return;
        }
        call("get_game_info", appId)
            .then(info => {
            const processedInfo = info?.error ? null : info;
            setGameInfo(processedInfo);
            gameInfoCache.set(appId, { info: processedInfo, timestamp: Date.now() });
        })
            .catch(() => setGameInfo(null));
    }, [appId]);
    // Poll for download state when we have game info
    SP_REACT.useEffect(() => {
        if (!gameInfo)
            return;
        const checkDownloadState = async () => {
            try {
                const result = await call("is_game_downloading", gameInfo.game_id, gameInfo.store);
                setDownloadState(prevState => {
                    const newState = {
                        isDownloading: false,
                        progress: 0,
                        downloadId: undefined
                    };
                    if (result.success && result.is_downloading && result.download_info) {
                        const status = result.download_info.status;
                        // Only show as downloading if status is actively downloading or queued
                        // Cancelled/error items should not be shown as active downloads
                        if (status === 'downloading' || status === 'queued') {
                            newState.isDownloading = true;
                            newState.progress = result.download_info.progress_percent;
                            newState.downloadId = result.download_info.id;
                        }
                        // If status is cancelled/error/completed, isDownloading stays false
                    }
                    // Detect transition from Downloading -> Not Downloading (Completion)
                    if (prevState.isDownloading && !newState.isDownloading) {
                        console.log("[InstallInfoDisplay] Download finished, refreshing game info...");
                        // Invalidate cache first to ensure fresh data
                        gameInfoCache.delete(appId);
                        // Refresh game info to update button state (Install -> Play/Uninstall)
                        call("get_game_info", appId)
                            .then(info => {
                            const processedInfo = info?.error ? null : info;
                            setGameInfo(processedInfo);
                            if (processedInfo) {
                                gameInfoCache.set(appId, { info: processedInfo, timestamp: Date.now() });
                            }
                        });
                    }
                    return newState;
                });
            }
            catch (error) {
                console.error("[InstallInfoDisplay] Error checking download state:", error);
            }
        };
        // Initial check
        checkDownloadState();
        // Poll every second when displaying
        pollIntervalRef.current = setInterval(checkDownloadState, 1000);
        return () => {
            if (pollIntervalRef.current) {
                clearInterval(pollIntervalRef.current);
            }
        };
    }, [gameInfo, appId]);
    const handleInstall = async () => {
        if (!gameInfo)
            return;
        setProcessing(true);
        // Queue download instead of direct install
        const result = await call("add_to_download_queue_by_appid", appId);
        if (result.success) {
            toaster.toast({
                title: "Download Started. Check UNIFIDECK > Downloads",
                body: `${gameInfo.title} has been added to the queue.`,
                duration: 5000,
            });
            // Show multi-part alert for GOG games with multiple installer parts
            if (result.is_multipart) {
                toaster.toast({
                    title: "Multi-Part Download Detected",
                    body: "Please be patient and wait for completion",
                    duration: 8000,
                });
            }
            // Force immediate state check to update UI to "Cancel" faster
            setDownloadState(prev => ({ ...prev, isDownloading: true, progress: 0 }));
        }
        else {
            toaster.toast({
                title: "Download Failed",
                body: result.error || "Could not start download.",
                duration: 10000,
                critical: true,
            });
        }
        setProcessing(false);
    };
    const handleCancel = async () => {
        // If we don't have a specific download ID yet (race condition at start), try to construct it
        const dlId = downloadState.downloadId || `${gameInfo.store}:${gameInfo.game_id}`;
        setProcessing(true);
        const result = await call("cancel_download_by_id", dlId);
        if (result.success) {
            toaster.toast({
                title: "Download Cancelled",
                body: `${gameInfo?.title} download cancelled.`,
                duration: 5000,
            });
            setDownloadState({ isDownloading: false, progress: 0 });
        }
        else {
            toaster.toast({
                title: "Cancel Failed",
                body: result.error || "Could not cancel download.",
                duration: 5000,
                critical: true,
            });
        }
        setProcessing(false);
    };
    const handleUninstall = async () => {
        if (!gameInfo)
            return;
        setProcessing(true);
        toaster.toast({
            title: "Uninstalling Game",
            body: `Removing ${gameInfo.title}...`,
            duration: 5000,
        });
        const result = await call("uninstall_game_by_appid", appId);
        if (result.success) {
            setGameInfo({ ...gameInfo, is_installed: false });
            gameInfoCache.delete(appId);
            toaster.toast({
                title: "Uninstallation Complete!",
                body: `${gameInfo.title} removed.`,
                duration: 10000,
            });
        }
        else {
            toaster.toast({
                title: "Uninstallation Failed",
                body: result.error || "Unknown error",
                duration: 10000,
                critical: true,
            });
        }
        setProcessing(false);
    };
    // Confirmation wrapper functions using native Steam modal
    const showInstallConfirmation = () => {
        DFL.showModal(SP_JSX.jsx(DFL.ConfirmModal, { strTitle: "Confirm Installation", strDescription: `Are you sure you want to install ${gameInfo?.title}?`, strOKButtonText: "Yes", strCancelButtonText: "No", onOK: () => handleInstall() }));
    };
    const showUninstallConfirmation = () => {
        DFL.showModal(SP_JSX.jsx(DFL.ConfirmModal, { strTitle: "Confirm Uninstallation", strDescription: `Are you sure you want to uninstall ${gameInfo?.title}?`, strOKButtonText: "Yes", strCancelButtonText: "No", bDestructiveWarning: true, onOK: () => handleUninstall() }));
    };
    const showCancelConfirmation = () => {
        DFL.showModal(SP_JSX.jsx(DFL.ConfirmModal, { strTitle: "Confirm Cancellation", strDescription: `Are you sure you want to cancel the download for ${gameInfo?.title}?`, strOKButtonText: "Yes", strCancelButtonText: "No", bDestructiveWarning: true, onOK: () => handleCancel() }));
    };
    // Not a Unifideck game - return null
    if (!gameInfo || gameInfo.error)
        return null;
    const isInstalled = gameInfo.is_installed;
    // Determine button display based on state
    let buttonText;
    let buttonAction;
    // Dynamic style based on state
    let buttonStyle = {
        padding: '8px 12px',
        minHeight: '42px',
        boxShadow: 'none',
        borderBottom: 'none',
    };
    if (downloadState.isDownloading) {
        // Show "Cancel" button with progress during active download
        // Use Math.max(0) to avoid negative -1 initialization
        const progress = Math.max(0, downloadState.progress || 0).toFixed(0);
        buttonText = `✖ Cancel (${progress}%)`;
        buttonAction = showCancelConfirmation;
        buttonStyle = {
            ...buttonStyle,
            backgroundColor: 'rgba(200, 40, 40, 0.4)', // Red tint for cancel
            border: '1px solid #ff4444',
        };
    }
    else if (isInstalled) {
        // Show size for installed games if available
        const sizeText = gameInfo.size_formatted ? ` (${gameInfo.size_formatted})` : ' (- GB)';
        buttonText = `Uninstall ${gameInfo.title}${sizeText}`;
        buttonAction = showUninstallConfirmation;
    }
    else {
        // Show size in Install button
        const sizeText = gameInfo.size_formatted ? ` (${gameInfo.size_formatted})` : ' (- GB)';
        buttonText = `⬇ Install ${gameInfo.title}${sizeText}`;
        buttonAction = showInstallConfirmation;
    }
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: ["      ", SP_JSX.jsx(DFL.Focusable, { style: {
                    position: 'absolute',
                    top: '40px', // Aligned with ProtonDB badge row
                    right: '35px',
                    zIndex: 1000,
                }, children: SP_JSX.jsx(DFL.DialogButton, { onClick: buttonAction, disabled: processing, style: buttonStyle, children: processing ? 'Processing...' : buttonText }) })] }));
};
// Patch function for game details route - EXTRACTED TO MODULE SCOPE (ProtonDB/HLTB pattern)
// This ensures the patch is registered in the correct Decky loader context
function patchGameDetailsRoute() {
    return routerHook.addPatch('/library/app/:appid', (routerTree) => {
        const routeProps = DFL.findInReactTree(routerTree, (x) => x?.renderFunc);
        if (!routeProps)
            return routerTree;
        // Create tree patcher (SAFE: mutates BEFORE React reconciles)
        const patchHandler = DFL.createReactTreePatcher([
            // Finder function: return children array (NOT overview object) - ProtonDB pattern
            (tree) => DFL.findInReactTree(tree, (x) => x?.props?.children?.props?.overview)?.props?.children
        ], (_, ret) => {
            // Patcher function: SAFE to mutate here (before reconciliation)
            // Extract appId from ret (not from finder closure)
            const overview = DFL.findInReactTree(ret, (x) => x?.props?.children?.props?.overview)?.props?.children?.props?.overview;
            if (!overview)
                return ret;
            const appId = overview.appid;
            try {
                // Strategy: Find the Header area (contains Play button and game info)
                // The Header is at the top of the game details page, above the scrollable content
                // Look for the AppDetailsHeader container first (best position)
                const headerContainer = DFL.findInReactTree(ret, (x) => Array.isArray(x?.props?.children) &&
                    (x?.props?.className?.includes(DFL.appDetailsClasses?.Header) ||
                        x?.props?.className?.includes(DFL.appDetailsHeaderClasses?.TopCapsule)));
                // Find the PlaySection container (where Play button lives)
                const playSection = DFL.findInReactTree(ret, (x) => Array.isArray(x?.props?.children) &&
                    x?.props?.className?.includes(DFL.playSectionClasses?.Container));
                // Alternative: Find the AppButtonsContainer
                const buttonsContainer = DFL.findInReactTree(ret, (x) => Array.isArray(x?.props?.children) &&
                    x?.props?.className?.includes(DFL.playSectionClasses?.AppButtonsContainer));
                // Find the game info row (typically contains play button, shortcuts, settings)
                const gameInfoRow = DFL.findInReactTree(ret, (x) => Array.isArray(x?.props?.children) &&
                    x?.props?.style?.display === 'flex' &&
                    x?.props?.children?.some?.((c) => c?.props?.className?.includes?.(DFL.appActionButtonClasses?.PlayButtonContainer) ||
                        c?.type?.toString?.()?.includes?.('PlayButton')));
                // Find InnerContainer as fallback (original approach)
                const innerContainer = DFL.findInReactTree(ret, (x) => Array.isArray(x?.props?.children) &&
                    x?.props?.className?.includes(DFL.appDetailsClasses?.InnerContainer));
                // ProtonDB COMPATIBILITY: Always use InnerContainer first to match ProtonDB's behavior
                // When multiple plugins modify the SAME container, patches chain correctly.
                // When plugins modify DIFFERENT containers (parent vs child), React reconciliation conflicts occur.
                // Since InstallInfoDisplay uses position: absolute, it works in any container.
                let container = innerContainer || headerContainer || playSection || buttonsContainer || gameInfoRow;
                // If none of those work, log but try to proceed with whatever we have (or return)
                if (!container) {
                    console.log(`[Unifideck] No suitable container found for app ${appId}, skipping injection`);
                    return ret;
                }
                // Ensure children is an array
                if (!Array.isArray(container.props.children)) {
                    container.props.children = [container.props.children];
                }
                // ProtonDB COMPATIBILITY: Insert at index 2
                // ProtonDB inserts at index 1. By inserting at index 2, we:
                // 1. Avoid overwriting ProtonDB's element
                // 2. Stay early in the children array so focus navigation works
                // Since InstallInfoDisplay uses position: absolute, its visual position is CSS-controlled.
                const spliceIndex = Math.min(2, container.props.children.length);
                // Inject our install info display after play button
                container.props.children.splice(spliceIndex, 0, SP_REACT.createElement(InstallInfoDisplay, {
                    key: `unifideck-install-info-${appId}`,
                    appId
                }));
                console.log(`[Unifideck] Injected install info for app ${appId} in ${innerContainer ? 'InnerContainer' : headerContainer ? 'Header' : playSection ? 'PlaySection' : buttonsContainer ? 'ButtonsContainer' : 'GameInfoRow'} at index ${spliceIndex}`);
            }
            catch (error) {
                console.error('[Unifideck] Error injecting install info:', error);
            }
            return ret; // Always return modified tree
        });
        // Apply patcher to renderFunc
        DFL.afterPatch(routeProps, 'renderFunc', patchHandler);
        return routerTree;
    });
}
// Settings panel in Quick Access Menu
const Content = () => {
    // Tab navigation state
    const [activeTab, setActiveTab] = SP_REACT.useState('settings');
    const [syncing, setSyncing] = SP_REACT.useState(false);
    const [syncCooldown, setSyncCooldown] = SP_REACT.useState(false);
    const [cooldownSeconds, setCooldownSeconds] = SP_REACT.useState(0);
    const [deleting, setDeleting] = SP_REACT.useState(false);
    const [showDeleteConfirm, setShowDeleteConfirm] = SP_REACT.useState(false);
    const [deleteFiles, setDeleteFiles] = SP_REACT.useState(false);
    // Auto-focus ref
    const mountRef = SP_REACT.useRef(null);
    // Auto-focus logic
    SP_REACT.useEffect(() => {
        // Focus the first focusable element on mount
        const timer = setTimeout(() => {
            if (mountRef.current) {
                const focusable = mountRef.current.querySelector('button, [tabindex="0"]');
                if (focusable instanceof HTMLElement) {
                    focusable.focus();
                    focusable.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
            }
        }, 100);
        return () => clearTimeout(timer);
    }, []);
    const [storeStatus, setStoreStatus] = SP_REACT.useState({
        epic: "Checking...",
        gog: "Checking...",
        amazon: "Checking...",
    });
    const [authDialog, setAuthDialog] = SP_REACT.useState({
        show: false,
        store: null,
        url: '',
        code: '',
        processing: false,
        error: '',
        autoMode: false
    });
    const [syncProgress, setSyncProgress] = SP_REACT.useState(null);
    // Store polling interval ref to allow cleanup on unmount
    const pollIntervalRef = SP_REACT.useRef(null);
    // Cleanup polling interval on unmount
    SP_REACT.useEffect(() => {
        return () => {
            if (pollIntervalRef.current) {
                clearInterval(pollIntervalRef.current);
                pollIntervalRef.current = null;
                console.log("[Unifideck] Cleaned up polling interval on unmount");
            }
        };
    }, []);
    SP_REACT.useEffect(() => {
        // Check store connectivity on mount
        checkStoreStatus();
    }, []);
    // Restore sync state on mount (in case user navigated away during sync)
    SP_REACT.useEffect(() => {
        const restoreSyncState = async () => {
            try {
                const status = await call("get_sync_status");
                if (status.is_syncing && status.sync_progress) {
                    console.log("[Unifideck] Restoring sync state on mount:", status.sync_progress);
                    // Restore syncing state
                    setSyncing(true);
                    setSyncProgress(status.sync_progress);
                    // Deduplication flag (scoped to this restore)
                    let completionHandled = false;
                    // Clear any existing polling interval before creating a new one
                    if (pollIntervalRef.current) {
                        clearInterval(pollIntervalRef.current);
                        console.log("[Unifideck] Cleared existing polling interval before restore");
                    }
                    // Resume polling for progress
                    pollIntervalRef.current = setInterval(async () => {
                        try {
                            const result = await call("get_sync_progress");
                            if (result.success) {
                                setSyncProgress(result);
                                // Log progress updates
                                if (result.current_game) {
                                    const progress = result.current_phase === 'artwork'
                                        ? `${result.artwork_synced}/${result.artwork_total}`
                                        : `${result.synced_games}/${result.total_games}`;
                                    console.log(`[Unifideck] ${result.current_game} (${progress})`);
                                }
                                // Stop polling when complete, error, or cancelled
                                if (result.status === 'complete' || result.status === 'error' || result.status === 'cancelled') {
                                    if (pollIntervalRef.current) {
                                        clearInterval(pollIntervalRef.current);
                                        pollIntervalRef.current = null;
                                    }
                                    setSyncing(false);
                                    // Only run completion logic once
                                    if (!completionHandled) {
                                        completionHandled = true;
                                        if (result.status === 'complete') {
                                            console.log(`[Unifideck] ✓ Sync completed: ${result.synced_games} games processed`);
                                        }
                                        else if (result.status === 'cancelled') {
                                            console.log(`[Unifideck] ⚠ Sync cancelled by user`);
                                        }
                                        // Show toast only if changes were made
                                        if (result.status === 'complete') {
                                            const addedCount = result.synced_games || 0;
                                            if (addedCount > 0) {
                                                toaster.toast({
                                                    title: "Sync Complete!",
                                                    body: `Added ${addedCount} games. RESTART STEAM (exit completely, not just return to game mode) to see them in your library.`,
                                                    duration: 15000,
                                                    critical: true,
                                                });
                                            }
                                        }
                                        else if (result.status === 'cancelled') {
                                            toaster.toast({
                                                title: "Sync Cancelled",
                                                body: result.current_game || "Sync was cancelled by user",
                                                duration: 5000,
                                            });
                                        }
                                        // Start cooldown
                                        setSyncCooldown(true);
                                        setCooldownSeconds(5);
                                        const cooldownInterval = setInterval(() => {
                                            setCooldownSeconds(prev => {
                                                if (prev <= 1) {
                                                    clearInterval(cooldownInterval);
                                                    setSyncCooldown(false);
                                                    return 0;
                                                }
                                                return prev - 1;
                                            });
                                        }, 1000);
                                        setTimeout(() => setSyncProgress(null), 5000);
                                    }
                                }
                            }
                        }
                        catch (error) {
                            console.error("[Unifideck] Error polling sync progress:", error);
                        }
                    }, 500);
                    console.log("[Unifideck] Sync state restored, polling resumed");
                }
                else {
                    console.log("[Unifideck] No active sync on mount");
                }
            }
            catch (error) {
                console.error("[Unifideck] Error restoring sync state:", error);
            }
        };
        restoreSyncState();
    }, []);
    const checkStoreStatus = async () => {
        try {
            // Add timeout wrapper
            const timeoutPromise = new Promise((_, reject) => setTimeout(() => reject(new Error('Status check timed out')), 10000));
            const checkPromise = call("check_store_status");
            const result = await Promise.race([checkPromise, timeoutPromise]);
            if (result.success) {
                setStoreStatus({
                    epic: result.epic,
                    gog: result.gog,
                    amazon: result.amazon
                });
                // Show warning if legendary not installed
                if (result.legendary_installed === false) {
                    console.warn("[Unifideck] Legendary CLI not installed - Epic Games won't work");
                }
                // Show warning if nile not installed
                if (result.nile_installed === false) {
                    console.warn("[Unifideck] Nile CLI not installed - Amazon Games won't work");
                }
            }
            else {
                console.error("[Unifideck] Status check failed:", result.error);
                setStoreStatus({
                    epic: "Error - Check logs",
                    gog: "Error - Check logs",
                    amazon: "Error - Check logs"
                });
            }
        }
        catch (error) {
            console.error("[Unifideck] Error checking store status:", error);
            setStoreStatus({
                epic: "Error - " + error.message,
                gog: "Error - " + error.message,
                amazon: "Error - " + error.message
            });
        }
    };
    const handleManualSync = async (force = false) => {
        // Prevent concurrent syncs
        if (syncing || syncCooldown) {
            console.log("[Unifideck] Sync already in progress or on cooldown");
            return;
        }
        setSyncing(true);
        setSyncProgress(null);
        // Deduplication flag to prevent multiple polls handling completion
        let completionHandled = false;
        // Clear any existing polling interval before creating a new one
        if (pollIntervalRef.current) {
            clearInterval(pollIntervalRef.current);
            console.log("[Unifideck] Cleared existing polling interval before manual sync");
        }
        // Start polling for progress
        pollIntervalRef.current = setInterval(async () => {
            try {
                const result = await call("get_sync_progress");
                if (result.success) {
                    setSyncProgress(result);
                    // Log progress updates
                    if (result.current_game) {
                        const progress = result.current_phase === 'artwork'
                            ? `${result.artwork_synced}/${result.artwork_total}`
                            : `${result.synced_games}/${result.total_games}`;
                        console.log(`[Unifideck] ${result.current_game} (${progress})`);
                    }
                }
                // Stop polling when complete, error, or cancelled
                if (result.status === 'complete' || result.status === 'error' || result.status === 'cancelled') {
                    if (pollIntervalRef.current) {
                        clearInterval(pollIntervalRef.current);
                        pollIntervalRef.current = null;
                    }
                    setSyncing(false);
                    // CRITICAL FIX: Only run completion logic ONCE
                    if (!completionHandled) {
                        completionHandled = true; // Set flag IMMEDIATELY
                        if (result.status === 'complete') {
                            console.log(`[Unifideck] ✓ Sync completed: ${result.synced_games} games processed`);
                        }
                        else if (result.status === 'cancelled') {
                            console.log(`[Unifideck] ⚠ Sync cancelled by user`);
                        }
                        // Show restart notification when sync completes (only if changes were made)
                        if (result.status === 'complete') {
                            // Only show toast if there were actual changes (not just a refresh that added 0 games)
                            const addedCount = result.synced_games || 0;
                            if (addedCount > 0) {
                                toaster.toast({
                                    title: force ? "Force Sync Complete!" : "Sync Complete!",
                                    body: force
                                        ? `Updated ${addedCount} games. RESTART STEAM to see changes.`
                                        : `Added ${addedCount} games. RESTART STEAM (exit completely, not just return to game mode) to see them in your library.`,
                                    duration: 15000,
                                    critical: true,
                                });
                            }
                        }
                        else if (result.status === 'cancelled') {
                            toaster.toast({
                                title: "Sync Cancelled",
                                body: result.current_game || "Sync was cancelled by user",
                                duration: 5000,
                            });
                        }
                    }
                    else {
                        // Completion already handled by another poll - do nothing
                        console.log(`[Unifideck] (duplicate poll detected, skipping completion logic)`);
                    }
                }
            }
            catch (error) {
                console.error("[Unifideck] Error getting sync progress:", error);
            }
        }, 500); // Poll every 500ms
        try {
            // Use force_sync_libraries for force sync (rewrites shortcuts and compatibility data)
            const methodName = force ? "force_sync_libraries" : "sync_libraries";
            console.log(`[Unifideck] Starting ${force ? 'force ' : ''}sync...`);
            const syncResult = await call(methodName);
            console.log("[Unifideck] ========== SYNC COMPLETED ==========");
            console.log(`[Unifideck] Epic Games: ${syncResult.epic_count}`);
            console.log(`[Unifideck] GOG Games: ${syncResult.gog_count}`);
            console.log(`[Unifideck] Amazon Games: ${syncResult.amazon_count || 0}`);
            console.log(`[Unifideck] Total Games: ${syncResult.epic_count + syncResult.gog_count + (syncResult.amazon_count || 0)}`);
            console.log(`[Unifideck] Games Added: ${syncResult.added_count}`);
            console.log(`[Unifideck] Artwork Fetched: ${syncResult.artwork_count}`);
            console.log("[Unifideck] =====================================");
            // Phase 3: Sync Steam Collections
            // Update collections ([Unifideck] Epic Games, etc.) with new games
            await syncUnifideckCollections().catch(err => console.error("[Unifideck] Failed to sync collections:", err));
            await checkStoreStatus();
        }
        catch (error) {
            console.error("[Unifideck] Manual sync failed:", error);
            if (pollIntervalRef.current) {
                clearInterval(pollIntervalRef.current);
                pollIntervalRef.current = null;
            }
            setSyncing(false);
        }
        finally {
            setSyncing(false);
            // START COOLDOWN
            setSyncCooldown(true);
            setCooldownSeconds(5);
            // Countdown timer
            const cooldownInterval = setInterval(() => {
                setCooldownSeconds(prev => {
                    if (prev <= 1) {
                        clearInterval(cooldownInterval);
                        setSyncCooldown(false);
                        return 0;
                    }
                    return prev - 1;
                });
            }, 1000);
            // Clear progress after cooldown
            setTimeout(() => setSyncProgress(null), 5000);
        }
    };
    /**
     * Poll store status to detect when authentication completes
     */
    const pollForAuthCompletion = async (store) => {
        const maxAttempts = 60; // 5 minutes (60 * 5s)
        let attempts = 0;
        // Helper function to check status
        const checkStatus = async () => {
            try {
                const result = await call("check_store_status");
                if (result.success) {
                    let status;
                    if (store === 'epic') {
                        status = result.epic;
                    }
                    else if (store === 'gog') {
                        status = result.gog;
                    }
                    else {
                        status = result.amazon;
                    }
                    if (status === "Connected") {
                        console.log(`[Unifideck] ${store} authentication completed automatically!`);
                        return true;
                    }
                }
            }
            catch (error) {
                console.error(`[Unifideck] Error polling status:`, error);
            }
            return false;
        };
        // Check immediately first (in case auth completed very fast)
        if (await checkStatus()) {
            return true;
        }
        return new Promise((resolve) => {
            const pollInterval = setInterval(async () => {
                attempts++;
                if (await checkStatus()) {
                    clearInterval(pollInterval);
                    resolve(true);
                    return;
                }
                // Timeout after max attempts
                if (attempts >= maxAttempts) {
                    clearInterval(pollInterval);
                    console.log(`[Unifideck] Polling timeout for ${store} authentication`);
                    resolve(false);
                }
            }, 5000); // Poll every 5 seconds
        });
    };
    const startAuth = async (store) => {
        try {
            let methodName;
            if (store === 'epic') {
                methodName = 'start_epic_auth';
            }
            else if (store === 'gog') {
                methodName = 'start_gog_auth_auto';
            }
            else {
                methodName = 'start_amazon_auth';
            }
            const result = await call(methodName);
            if (result.success && result.url) {
                const authUrl = result.url;
                // Open popup window
                const popup = window.open(authUrl, '_blank', 'width=800,height=600,popup=yes');
                if (!popup) {
                    setAuthDialog({
                        show: true,
                        store,
                        url: authUrl,
                        code: '',
                        processing: false,
                        error: 'Failed to open popup window - popup may be blocked',
                        autoMode: false
                    });
                    return;
                }
                console.log(`[Unifideck] Opened ${store} auth popup. Backend monitoring via CDP...`);
                // Show dialog indicating we're waiting
                setAuthDialog({
                    show: true,
                    store,
                    url: authUrl,
                    code: '',
                    processing: true,
                    error: '',
                    autoMode: true
                });
                // Poll for authentication completion
                const completed = await pollForAuthCompletion(store);
                if (completed) {
                    toaster.toast({
                        title: "Auth Successful",
                        body: "You can close the window",
                        duration: 5000,
                    });
                    setAuthDialog({ show: false, store: null, url: '', code: '', processing: false, error: '', autoMode: false });
                    await checkStoreStatus(); // Refresh status
                }
                else {
                    setAuthDialog(prev => ({
                        ...prev,
                        processing: false,
                        error: 'Authentication timeout - please check logs or try again'
                    }));
                }
            }
            else {
                setAuthDialog({
                    show: true,
                    store,
                    url: '',
                    code: '',
                    processing: false,
                    error: result.error || 'Failed to start authentication',
                    autoMode: false
                });
            }
        }
        catch (error) {
            console.error(`[Unifideck] Error starting ${store} auth:`, error);
            setAuthDialog({
                show: true,
                store,
                url: '',
                code: '',
                processing: false,
                error: `Error: ${error.message || error}`,
                autoMode: false
            });
        }
    };
    const handleLogout = async (store) => {
        try {
            let methodName;
            if (store === 'epic') {
                methodName = 'logout_epic';
            }
            else if (store === 'gog') {
                methodName = 'logout_gog';
            }
            else {
                methodName = 'logout_amazon';
            }
            const result = await call(methodName);
            if (result.success) {
                console.log(`[Unifideck] Logged out from ${store}`);
                await checkStoreStatus();
            }
        }
        catch (error) {
            console.error(`[Unifideck] Error logging out from ${store}:`, error);
        }
    };
    const handleDeleteAll = async () => {
        if (!showDeleteConfirm) {
            setShowDeleteConfirm(true);
            return;
        }
        setDeleting(true);
        setShowDeleteConfirm(false);
        try {
            const result = await call("perform_full_cleanup", { delete_files: deleteFiles });
            // Reset checkbox
            setDeleteFiles(false);
            if (result.success) {
                console.log(`[Unifideck] Cleanup complete: ${result.deleted_games} games, ` +
                    `${result.deleted_artwork} artwork sets, ${result.deleted_files_count} files deleted`);
                toaster.toast({
                    title: "Cleanup Successful",
                    body: `Removed ${result.deleted_games} games, ${result.deleted_artwork} artwork sets, ` +
                        `and ${result.deleted_files_count} file directories. Auth & cache cleared.`,
                    duration: 8000,
                });
            }
            else {
                console.error(`[Unifideck] Delete failed: ${result.error}`);
                toaster.toast({
                    title: "Delete Failed",
                    body: result.error || "Unknown error",
                    duration: 5000,
                });
            }
        }
        catch (error) {
            console.error("[Unifideck] Delete error:", error);
        }
        finally {
            setDeleting(false);
        }
    };
    const handleCancelSync = async () => {
        try {
            // Clear polling interval immediately when user cancels
            if (pollIntervalRef.current) {
                clearInterval(pollIntervalRef.current);
                pollIntervalRef.current = null;
                console.log("[Unifideck] Cleared polling interval on user cancel");
            }
            // Clear progress bar immediately
            setSyncProgress(null);
            setSyncing(false);
            const result = await call("cancel_sync");
            if (result.success) {
                console.log("[Unifideck] Sync cancelled");
                toaster.toast({
                    title: "UNIFIDECK SYNC CANCELED",
                    body: "",
                    duration: 3000,
                });
            }
            else {
                console.log("[Unifideck] Cancel failed:", result.message);
            }
        }
        catch (error) {
            console.error("[Unifideck] Error cancelling sync:", error);
        }
    };
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { ref: mountRef, style: { width: "100%" }, children: SP_JSX.jsxs(DFL.Focusable, { style: {
                                display: "flex",
                                flexDirection: "column",
                                gap: "4px",
                                width: "100%",
                            }, children: [SP_JSX.jsx(DFL.DialogButton, { onClick: () => setActiveTab('settings'), style: {
                                        width: "100%",
                                        padding: "8px 12px",
                                        fontSize: "13px",
                                        backgroundColor: activeTab === 'settings' ? '#1a9fff' : 'transparent',
                                        border: activeTab === 'settings' ? 'none' : '1px solid #444',
                                        borderRadius: '4px',
                                        fontWeight: activeTab === 'settings' ? 'bold' : 'normal',
                                        textAlign: "left",
                                        justifyContent: "flex-start",
                                        display: "flex",
                                        alignItems: "center",
                                    }, children: "\u2699\uFE0F Settings" }), SP_JSX.jsx(DFL.DialogButton, { onClick: () => setActiveTab('downloads'), style: {
                                        width: "100%",
                                        padding: "8px 12px",
                                        fontSize: "13px",
                                        backgroundColor: activeTab === 'downloads' ? '#1a9fff' : 'transparent',
                                        border: activeTab === 'downloads' ? 'none' : '1px solid #444',
                                        borderRadius: '4px',
                                        fontWeight: activeTab === 'downloads' ? 'bold' : 'normal',
                                        textAlign: "left",
                                        justifyContent: "flex-start",
                                        display: "flex",
                                        alignItems: "center",
                                    }, children: "\u2B07\uFE0F Downloads" })] }) }) }) }), activeTab === 'downloads' && (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DownloadsTab, {}), SP_JSX.jsx(StorageSettings, {})] })), activeTab === 'settings' && (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSection, { title: "Unifideck Settings", children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { display: "flex", flexDirection: "column", gap: "10px" }, children: [SP_JSX.jsx("div", { children: "Add Epic, GOG, and Amazon games to your Steam Deck library." }), SP_JSX.jsx("div", { style: { fontSize: "12px", opacity: 0.7 }, children: "All your games under one roof." })] }) }) }), SP_JSX.jsxs(DFL.PanelSection, { title: "Epic Games", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }, children: SP_JSX.jsxs("div", { style: { fontSize: "14px" }, children: ["Status: ", storeStatus.epic === "Connected" ? "✓ Connected" :
                                                storeStatus.epic === "Legendary not installed" ? "⚠️ Installing..." :
                                                    storeStatus.epic === "Checking..." ? "Checking..." :
                                                        storeStatus.epic.includes("Error") ? `❌ ${storeStatus.epic}` :
                                                            "✗ Not Connected"] }) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { children: storeStatus.epic === "Connected" ? (SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => handleLogout('epic'), children: SP_JSX.jsx("div", { style: { fontSize: "0.85em", padding: "2px" }, children: "Logout" }) })) : storeStatus.epic !== "Checking..." && !storeStatus.epic.includes("Error") && storeStatus.epic !== "Legendary not installed" ? (SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => startAuth('epic'), children: SP_JSX.jsx("div", { style: { fontSize: "0.85em", padding: "2px" }, children: "Authenticate" }) })) : null }) }), storeStatus.epic === "Legendary not installed" && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: '11px', opacity: 0.7 }, children: "Installing legendary CLI automatically..." }) }))] }), SP_JSX.jsxs(DFL.PanelSection, { title: "GOG", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }, children: SP_JSX.jsxs("div", { style: { fontSize: "14px" }, children: ["Status: ", storeStatus.gog === "Connected" ? "✓ Connected" :
                                                storeStatus.gog === "Checking..." ? "Checking..." :
                                                    storeStatus.gog.includes("Error") ? `❌ ${storeStatus.gog}` :
                                                        "✗ Not Connected"] }) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { children: storeStatus.gog === "Connected" ? (SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => handleLogout('gog'), children: SP_JSX.jsx("div", { style: { fontSize: "0.85em", padding: "2px" }, children: "Logout" }) })) : storeStatus.gog !== "Checking..." && !storeStatus.gog.includes("Error") ? (SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => startAuth('gog'), children: SP_JSX.jsx("div", { style: { fontSize: "0.85em", padding: "2px" }, children: "Authenticate" }) })) : null }) })] }), SP_JSX.jsxs(DFL.PanelSection, { title: "AMAZON GAMES", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }, children: SP_JSX.jsxs("div", { style: { fontSize: "14px" }, children: ["Status: ", storeStatus.amazon === "Connected" ? "✓ Connected" :
                                                storeStatus.amazon === "Nile not installed" ? "⚠️ Missing CLI" :
                                                    storeStatus.amazon === "Checking..." ? "Checking..." :
                                                        storeStatus.amazon.includes("Error") ? `❌ ${storeStatus.amazon}` :
                                                            "✗ Not Connected"] }) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { children: storeStatus.amazon === "Connected" ? (SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => handleLogout('amazon'), children: SP_JSX.jsx("div", { style: { fontSize: "0.85em", padding: "2px" }, children: "Logout" }) })) : storeStatus.amazon !== "Checking..." && !storeStatus.amazon.includes("Error") && storeStatus.amazon !== "Nile not installed" ? (SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => startAuth('amazon'), children: SP_JSX.jsx("div", { style: { fontSize: "0.85em", padding: "2px" }, children: "Authenticate" }) })) : null }) }), storeStatus.amazon === "Nile not installed" && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: '11px', opacity: 0.7 }, children: "Nile CLI not found. Amazon Games unavailable." }) }))] }), SP_JSX.jsxs(DFL.PanelSection, { title: "LIBRARY SYNC", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => handleManualSync(false), disabled: syncing || syncCooldown, children: SP_JSX.jsxs("div", { style: {
                                            display: "flex",
                                            alignItems: "center",
                                            gap: "2px",
                                            justifyContent: "center",
                                            fontSize: "0.85em",
                                            padding: "2px"
                                        }, children: [SP_JSX.jsx(FaSync, { style: {
                                                    animation: syncing ? "spin 1s linear infinite" : "none",
                                                    opacity: syncCooldown ? 0.5 : 1,
                                                    fontSize: "10px"
                                                } }), syncing
                                                ? "Syncing..."
                                                : syncCooldown
                                                    ? `${cooldownSeconds}s`
                                                    : "Sync Libraries"] }) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => handleManualSync(true), disabled: syncing || syncCooldown, children: SP_JSX.jsxs("div", { style: {
                                            display: "flex",
                                            alignItems: "center",
                                            gap: "2px",
                                            justifyContent: "center",
                                            color: "#ff9800",
                                            fontSize: "0.85em",
                                            padding: "2px"
                                        }, children: [SP_JSX.jsx(FaSync, { style: {
                                                    animation: syncing ? "spin 1s linear infinite" : "none",
                                                    opacity: syncCooldown ? 0.5 : 1,
                                                    fontSize: "10px"
                                                } }), syncing
                                                ? "..."
                                                : syncCooldown
                                                    ? `${cooldownSeconds}s`
                                                    : "Force Sync"] }) }) }), syncing && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: handleCancelSync, children: SP_JSX.jsx("div", { style: { display: "flex", alignItems: "center", gap: "8px", color: "#ff6b6b" }, children: "Cancel Sync" }) }) })), syncProgress && syncProgress.status !== 'idle' && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { fontSize: '12px', width: '100%' }, children: [SP_JSX.jsx("div", { style: { marginBottom: '5px', opacity: 0.9 }, children: syncProgress.current_game }), SP_JSX.jsx("div", { style: {
                                                width: '100%',
                                                height: '4px',
                                                backgroundColor: '#333',
                                                borderRadius: '2px',
                                                overflow: 'hidden'
                                            }, children: SP_JSX.jsx("div", { style: {
                                                    width: `${syncProgress.progress_percent}%`,
                                                    height: '100%',
                                                    backgroundColor: syncProgress.status === 'error' ? '#ff6b6b' :
                                                        syncProgress.status === 'complete' ? '#4caf50' :
                                                            syncProgress.current_phase === 'artwork' ? '#ff9800' : // Orange for artwork
                                                                '#1a9fff', // Blue for sync
                                                    transition: 'width 0.3s ease'
                                                } }) }), SP_JSX.jsx("div", { style: { marginTop: '5px', opacity: 0.7 }, children: syncProgress.current_phase === 'artwork' ? (
                                            // Artwork phase: show artwork progress
                                            SP_JSX.jsxs(SP_JSX.Fragment, { children: [syncProgress.artwork_synced, " / ", syncProgress.artwork_total, " artwork downloaded"] })) : (
                                            // Sync phase: show game progress
                                            SP_JSX.jsxs(SP_JSX.Fragment, { children: [syncProgress.synced_games, " / ", syncProgress.total_games, " games synced"] })) }), syncProgress.error && (SP_JSX.jsxs("div", { style: { color: '#ff6b6b', marginTop: '5px' }, children: ["Error: ", syncProgress.error] }))] }) })), (storeStatus.epic.includes("Error") || storeStatus.gog.includes("Error")) && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: checkStoreStatus, children: "Retry Status Check" }) }))] }), SP_JSX.jsx(DFL.PanelSection, { title: "Cleanup", children: !showDeleteConfirm ? (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: handleDeleteAll, disabled: syncing || deleting || syncCooldown, children: SP_JSX.jsxs("div", { style: { display: "flex", alignItems: "center", gap: "2px", fontSize: "0.85em", padding: "2px" }, children: [SP_JSX.jsx(FaTrash, { style: { fontSize: "10px" } }), "Delete all UNIFIDECK Libraries and Cache"] }) }) })) : (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { color: "#ff6b6b", fontWeight: "bold" }, children: "Are you sure? This will delete ALL Unifideck games, artwork, auth tokens, and cache. This action is irreversible." }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: {
                                            display: "flex",
                                            alignItems: "center",
                                            gap: "10px",
                                            margin: "10px 0"
                                        }, children: SP_JSX.jsx(DFL.ToggleField, { label: "Also delete installed game files? (Destructive)", checked: deleteFiles, onChange: (checked) => setDeleteFiles(checked) }) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: handleDeleteAll, disabled: deleting, children: SP_JSX.jsx("div", { style: { color: "#ff6b6b", fontSize: "0.85em", padding: "2px" }, children: deleting ? "Deleting..." : "Yes, Delete Everything" }) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => {
                                            setShowDeleteConfirm(false);
                                            setDeleteFiles(false);
                                        }, disabled: deleting, children: SP_JSX.jsx("div", { style: { fontSize: "0.85em", padding: "2px" }, children: "Cancel" }) }) })] })) }), authDialog.show && (SP_JSX.jsx("div", { style: {
                            position: 'fixed',
                            top: 0,
                            left: 0,
                            right: 0,
                            bottom: 0,
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            zIndex: 10000
                        }, children: SP_JSX.jsxs("div", { style: {
                                backgroundColor: '#1e2329',
                                padding: '20px',
                                borderRadius: '8px',
                                maxWidth: '500px',
                                width: '90%',
                                maxHeight: '80vh',
                                overflow: 'auto'
                            }, children: [SP_JSX.jsxs("h2", { style: { marginTop: 0 }, children: [authDialog.store === 'epic' ? 'Epic Games' : authDialog.store === 'amazon' ? 'Amazon Games' : 'GOG', " Authentication"] }), SP_JSX.jsxs("div", { children: [SP_JSX.jsx("div", { style: { marginBottom: '15px', fontSize: '14px' }, children: authDialog.processing ? (SP_JSX.jsxs("div", { children: [SP_JSX.jsx("p", { children: "Please complete the login in the popup window." }), SP_JSX.jsx("p", { style: { fontSize: '0.9em', color: '#888', marginTop: '5px' }, children: "The window will close automatically after authentication." }), SP_JSX.jsxs("div", { style: { marginTop: '20px', textAlign: 'center' }, children: [SP_JSX.jsx("div", { style: { fontSize: '32px' }, children: "\u23F3" }), SP_JSX.jsx("p", { style: { fontSize: '12px', opacity: 0.7, marginTop: '10px' }, children: "Waiting for authentication..." })] })] })) : (SP_JSX.jsx("div", { children: SP_JSX.jsx("p", { children: "\u2713 Ready to authenticate" }) })) }), authDialog.error && (SP_JSX.jsxs("div", { style: {
                                                marginBottom: '15px',
                                                padding: '10px',
                                                backgroundColor: '#5c1f1f',
                                                borderRadius: '4px',
                                                fontSize: '13px'
                                            }, children: [authDialog.error, authDialog.error.includes('cross-origin') && (SP_JSX.jsx("p", { style: { fontSize: '0.8em', marginTop: '5px' }, children: "The popup closed before authentication could complete. Please try again." }))] })), SP_JSX.jsx("div", { style: { display: 'flex', gap: '10px' }, children: SP_JSX.jsx("button", { onClick: () => setAuthDialog({ show: false, store: null, url: '', code: '', processing: false, error: '', autoMode: false }), style: {
                                                    flex: 1,
                                                    padding: '10px',
                                                    backgroundColor: '#3d4450',
                                                    border: 'none',
                                                    borderRadius: '4px',
                                                    color: 'white',
                                                    cursor: 'pointer'
                                                }, children: authDialog.processing ? 'Cancel' : 'Close' }) })] })] }) }))] }))] }));
};
var index = definePlugin(() => {
    console.log("[Unifideck] Plugin loaded");
    // Patch the library to add Unifideck tabs (All, Installed, Great on Deck, Steam, Epic, GOG, Amazon)
    // This uses TabMaster's approach: intercept useMemo hook to inject custom tabs
    const libraryPatch = patchLibrary();
    console.log("[Unifideck] ✓ Library tabs patch registered");
    // Patch game details route to inject Install button for uninstalled games
    // v70.3 FIX: Call extracted function to ensure proper Decky loader context
    const patchGameDetails = patchGameDetailsRoute();
    console.log("[Unifideck] ✓ All route patches registered (including game details)");
    // Sync Unifideck Collections on load (with delay to ensure Steam is ready)
    setTimeout(async () => {
        console.log("[Unifideck] Triggering initial collection sync...");
        try {
            await syncUnifideckCollections();
            console.log("[Unifideck] ✓ Initial collection sync complete");
        }
        catch (err) {
            console.error("[Unifideck] Initial collection sync failed:", err);
        }
    }, 5000); // 5 second delay to ensure Steam is fully loaded
    // Inject CSS AFTER patches with delay to ensure patches are active
    setTimeout(() => {
        console.log("[Unifideck] Hiding original tabs with CSS");
        const styleElement = document.createElement("style");
        styleElement.id = "unifideck-tab-hider";
        styleElement.textContent = `
      /* Hide original Steam library tabs */
      .library-tabs .tab[data-tab-id="all"],
      .library-tabs .tab[data-tab-id="great-on-deck"],
      .library-tabs .tab[data-tab-id="installed"] {
        display: none !important;
      visibility: hidden !important;
      }

      .library-tabs .tab[data-tab-id="all"].Focusable,
      .library-tabs .tab[data-tab-id="great-on-deck"].Focusable,
      .library-tabs .tab[data-tab-id="installed"].Focusable {
        display: none !important;
      pointer-events: none !important;
      }

      /* Hide navigation links */
      [href="/library/all"],
      [href="/library/great-on-deck"],
      [href="/library/installed"] {
        display: none !important;
      }

      /* Spinning animation for loading indicator */
      @keyframes spin {
        from {transform: rotate(0deg); }
      to {transform: rotate(360deg); }
      }

      .spinning {
        animation: spin 1s linear infinite;
      }
      `;
        document.head.appendChild(styleElement);
        console.log("[Unifideck] ✓ CSS injection complete");
    }, 100); // 100ms delay to ensure patches are active
    // Background sync disabled - users manually sync via UI when needed
    console.log("[Unifideck] Background sync disabled (use manual sync button)");
    return {
        name: "UNIFIDECK",
        icon: SP_JSX.jsx(FaGamepad, {}),
        content: SP_JSX.jsx(Content, {}),
        onDismount() {
            console.log("[Unifideck] Plugin unloading");
            // Remove CSS injection
            const styleEl = document.getElementById("unifideck-tab-hider");
            if (styleEl) {
                styleEl.remove();
            }
            // Remove route patches
            routerHook.removePatch("/library", libraryPatch);
            routerHook.removePatch("/library/app/:appid", patchGameDetails);
            // Clear game info cache
            gameInfoCache.clear();
            // Stop background sync service
            call("stop_background_sync")
                .catch((error) => console.error("[Unifideck] Failed to stop background sync:", error));
        },
    };
});

export { index as default };
//# sourceMappingURL=index.js.map
