"""unifiDB IGDB metadata fetcher via jsDelivr CDN."""
import json
import asyncio
import logging
import ssl
import certifi
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, TYPE_CHECKING
from urllib.parse import quote

try:
    import aiohttp
except ImportError:
    aiohttp = None

if TYPE_CHECKING:
    import aiohttp as aiohttp_types

logger = logging.getLogger(__name__)

# unifiDB CDN endpoint
UNIFIDB_CDN_BASE = "https://cdn.jsdelivr.net/gh/mubaraknumann/unifiDB@main"

# HTTP session for CDN requests (reused for efficiency)
_cdn_session = None  # type: Optional[aiohttp.ClientSession]

# IGDB store category mappings
IGDB_STORE_CATEGORIES = {
    1: 'steam',
    5: 'gog',
    26: 'epic',
    20: 'amazon',
    30: 'itch'
}


def normalize_title_for_unifidb(title: str) -> str:
    """Normalize game title for unifiDB bucket calculation."""
    if not title:
        return '00'
    
    normalized = title.lower()
    # Remove special characters, keep only alphanumeric
    normalized = ''.join(c if c.isalnum() else '' for c in normalized)
    
    if not normalized:
        return '00'
    
    # Return first 2 characters as bucket key
    bucket = normalized[:2].lower()
    return bucket


def get_first_char_for_bucket(bucket: str) -> str:
    """Get directory character from bucket."""
    if not bucket:
        return '0'
    first = bucket[0]
    if first.isalnum():
        return first
    return '0'


def score_title_match(normalized_search: str, normalized_game_name: str) -> float:
    """Score how well a game name matches the search.
    
    Returns:
        1.0 for exact match, 0.8 for substring, 0.6 for similar, 0.0 for no match
    """
    # Exact match
    if normalized_search == normalized_game_name:
        return 1.0
    
    # One contains the other
    if normalized_search in normalized_game_name or normalized_game_name in normalized_search:
        return 0.8
    
    # Check if all search words appear in game name (partial match)
    search_words = set(normalized_search.split())
    game_words = set(normalized_game_name.split())
    
    if search_words and search_words.issubset(game_words):
        return 0.6
    
    return 0.0


def normalize_title_for_matching(title: str) -> str:
    """Normalize a game title for fuzzy matching."""
    t = title.lower().strip()
    
    # Remove subtitles after common separators
    for sep in [' - ', ': ', ' – ', '™', '®']:
        if sep in t:
            t = t.split(sep)[0].strip()
    
    # Remove common edition suffixes
    suffixes = [
        'definitive edition',
        'complete edition',
        'goty edition',
        'game of the year edition',
        'deluxe edition',
        'ultimate edition',
        'gold edition',
        'anniversary edition',
        'remastered',
        'enhanced edition',
    ]
    
    for suffix in suffixes:
        if t.endswith(suffix):
            t = t[:-len(suffix)].strip()
    
    # Remove punctuation and extra whitespace
    t = ''.join(c if c.isalnum() or c.isspace() else '' for c in t)
    t = ' '.join(t.split())  # Normalize whitespace
    
    return t


async def search_unifidb_local(game_title: str, unifidb_path: Path) -> List[Tuple[float, Dict[str, Any]]]:
    """Search local unifiDB for games matching title.
    
    Args:
        game_title: Title of the game to search for
        unifidb_path: Path to local unifiDB directory
        
    Returns:
        List of (score, game_dict) tuples sorted by score descending
    """
    # Calculate bucket
    bucket = normalize_title_for_unifidb(game_title)
    first_char = get_first_char_for_bucket(bucket)
    
    # Load bucket file
    bucket_file = unifidb_path / "games" / first_char / f"{bucket}.json"
    
    if not bucket_file.exists():
        logger.debug(f"[unifiDB] Bucket file not found: {bucket_file}")
        return []
    
    try:
        with open(bucket_file, 'r') as f:
            games = json.load(f)
    except Exception as e:
        logger.error(f"[unifiDB] Error loading bucket {bucket}: {e}")
        return []
    
    # Score matches
    search_norm = normalize_title_for_matching(game_title)
    matches = []
    
    for game in games:
        if not isinstance(game, dict) or 'name' not in game:
            continue
        
        game_norm = normalize_title_for_matching(game['name'])
        score = score_title_match(search_norm, game_norm)
        
        if score > 0.0:
            matches.append((score, game))
    
    # Sort by score descending
    matches.sort(key=lambda x: -x[0])
    return matches


def extract_store_id_from_external_ids(game: Dict[str, Any], store: str) -> Optional[str]:
    """Extract store-specific game ID from external_ids array.
    
    Args:
        game: unifiDB game record
        store: Store name ('steam', 'gog', 'epic', 'amazon')
        
    Returns:
        Store-specific game ID or None if not found
    """
    external_ids = game.get('external_ids', [])
    
    for entry in external_ids:
        if isinstance(entry, dict) and entry.get('store') == store:
            return entry.get('uid')
    
    return None


def get_best_match(
    game_title: str,
    matches: List[Tuple[float, Dict[str, Any]]],
    required_stores: Optional[List[str]] = None
) -> Optional[Dict[str, Any]]:
    """Get the best matching game, with optional store filtering.
    
    Args:
        game_title: Original game title (for logging)
        matches: List of (score, game) tuples
        required_stores: List of store names that game must have IDs for (optional)
        
    Returns:
        Best matching game dict, or None if no good match found
    """
    if not matches:
        return None
    
    for score, game in matches:
        # If score is too low, skip
        if score < 0.6:
            break
        
        # If we need specific stores, verify they exist
        if required_stores:
            has_all_stores = all(
                extract_store_id_from_external_ids(game, store) 
                for store in required_stores
            )
            if not has_all_stores:
                continue
        
        logger.debug(f"[unifiDB] Best match for '{game_title}': '{game.get('name')}' (score={score:.2f})")
        return game
    
    logger.debug(f"[unifiDB] No acceptable match found for '{game_title}'")
    return None


def unifidb_game_to_cache_format(game: Dict[str, Any]) -> Dict[str, Any]:
    """Convert unifiDB game record to cache format.
    
    Args:
        game: Raw unifiDB game record
        
    Returns:
        Standardized metadata dict for caching
    """
    from datetime import datetime
    
    # Convert Unix timestamp to ISO date
    release_date = ''
    if 'release_date' in game and isinstance(game['release_date'], (int, float)):
        try:
            release_date = datetime.utcfromtimestamp(game['release_date']).strftime('%Y-%m-%d')
        except (ValueError, OSError):
            pass
    
    return {
        'igdb_id': game.get('igdb_id'),
        'name': game.get('name', ''),
        'description': game.get('summary', ''),
        'genres': game.get('genres', []),
        'developers': game.get('developers', []),
        'publishers': game.get('publishers', []),
        'released': release_date,
        'platforms': game.get('platforms', []),
        'cover_url': game.get('cover_url', ''),
        'external_ids': game.get('external_ids', []),
    }


async def _get_cdn_session():
    """Get or create CDN HTTP session with proper SSL context."""
    global _cdn_session
    if aiohttp is None:
        raise ImportError("aiohttp is required for CDN fetching")
    
    if _cdn_session is None or _cdn_session.closed:
        # Create SSL context with certifi certificates to avoid SSL verification errors
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context, limit_per_host=10)
        _cdn_session = aiohttp.ClientSession(connector=connector)
    
    return _cdn_session


async def close_cdn_session():
    """Close the CDN HTTP session."""
    global _cdn_session
    if _cdn_session and not _cdn_session.closed:
        await _cdn_session.close()
        _cdn_session = None


async def search_unifidb_cdn(
    game_title: str,
    timeout: float = 10.0
) -> List[Tuple[float, Dict[str, Any]]]:
    """Search unifiDB via CDN for games matching title.
    
    This fetches the bucket JSON file from jsDelivr CDN and searches for matches.
    
    Args:
        game_title: Title of the game to search for
        timeout: Request timeout in seconds
        
    Returns:
        List of (score, game_dict) tuples sorted by score descending
    """
    if aiohttp is None:
        logger.error("[unifiDB CDN] aiohttp not available")
        return []
    
    # Calculate bucket
    bucket = normalize_title_for_unifidb(game_title)
    first_char = get_first_char_for_bucket(bucket)
    
    # Build CDN URL
    cdn_url = f"{UNIFIDB_CDN_BASE}/games/{first_char}/{bucket}.json"
    
    try:
        session = await _get_cdn_session()
        
        # Don't pass ssl= here since we already configured SSL context in the connector
        async with session.get(
            cdn_url,
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status == 404:
                logger.debug(f"[unifiDB CDN] Bucket not found: {bucket}")
                return []
            
            if resp.status != 200:
                logger.warning(f"[unifiDB CDN] HTTP {resp.status} for bucket {bucket}")
                return []
            
            games = await resp.json()
    
    except asyncio.TimeoutError:
        logger.warning(f"[unifiDB CDN] Timeout fetching bucket {bucket}")
        return []
    except Exception as e:
        logger.error(f"[unifiDB CDN] Error fetching bucket {bucket}: {e}")
        return []
    
    # Score matches
    search_norm = normalize_title_for_matching(game_title)
    matches = []
    
    for game in games:
        if not isinstance(game, dict) or 'name' not in game:
            continue
        
        game_norm = normalize_title_for_matching(game['name'])
        score = score_title_match(search_norm, game_norm)
        
        if score > 0.0:
            matches.append((score, game))
    
    # Sort by score descending
    matches.sort(key=lambda x: -x[0])
    return matches


async def fetch_unifidb_metadata(
    game_title: str,
    timeout: float = 10.0
) -> Optional[Dict[str, Any]]:
    """Fetch unifiDB metadata for a single game via CDN.
    
    This is the main entry point for CDN-based unifiDB lookups.
    
    Args:
        game_title: Name of the game to search for
        timeout: Request timeout in seconds
        
    Returns:
        Standardized metadata dict or None if not found
    """
    try:
        matches = await search_unifidb_cdn(game_title, timeout=timeout)
        best_match = get_best_match(game_title, matches)
        
        if best_match:
            logger.info(f"[unifiDB CDN] Found metadata for: {game_title}")
            return unifidb_game_to_cache_format(best_match)
        else:
            logger.debug(f"[unifiDB CDN] No match for: {game_title}")
            return None
    
    except Exception as e:
        logger.error(f"[unifiDB CDN] Error fetching {game_title}: {e}")
        return None
