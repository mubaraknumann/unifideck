"""RAWG.io metadata fetching utilities."""

import logging
import urllib.parse
from typing import Dict, Any

logger = logging.getLogger(__name__)


# RAWG.io API key for fallback metadata
RAWG_API_KEY = 'ba1f3b6abe404ba993d6ac12479f2977'


async def fetch_rawg_metadata(game_name: str) -> Dict[str, Any]:
    """Fetch game metadata from RAWG.io as fallback.
    
    Args:
        game_name: Name of the game to search for
        
    Returns:
        Dict with: name, description, genres, tags, developers, publishers, 
                   metacritic, website, released
    """
    try:
        import aiohttp
        
        # Search for game by name
        search_url = f"https://api.rawg.io/api/games?key={RAWG_API_KEY}&search={urllib.parse.quote(game_name)}&page_size=1"
        
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    logger.debug(f"[RAWG] Search failed with status {response.status}")
                    return {}
                
                search_data = await response.json()
                
        results = search_data.get('results', [])
        if not results:
            logger.debug(f"[RAWG] No results for '{game_name}'")
            return {}
        
        game = results[0]
        game_id = game.get('id')
        
        # Get detailed game info
        detail_url = f"https://api.rawg.io/api/games/{game_id}?key={RAWG_API_KEY}"
        
        connector2 = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector2) as session:
            async with session.get(detail_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    # Return basic info from search if detail fails
                    return {
                        'name': game.get('name', ''),
                        'description': '',
                        'genres': [g.get('name', '') for g in game.get('genres', [])],
                        'tags': [t.get('name', '') for t in game.get('tags', [])[:10]],
                        'metacritic': game.get('metacritic'),
                        'released': game.get('released', ''),
                    }
                
                detail = await response.json()
        
        result = {
            'name': detail.get('name', ''),
            'description': detail.get('description_raw', ''),
            'genres': [g.get('name', '') for g in detail.get('genres', [])],
            'tags': [t.get('name', '') for t in detail.get('tags', [])[:10]],
            'developers': [d.get('name', '') for d in detail.get('developers', [])],
            'publishers': [p.get('name', '') for p in detail.get('publishers', [])],
            'metacritic': detail.get('metacritic'),
            'website': detail.get('website', ''),
            'released': detail.get('released', ''),
        }
        
        logger.info(f"[RAWG] Got metadata for '{game_name}': metacritic={result.get('metacritic')}, {len(result.get('tags', []))} tags")
        return result
            
    except Exception as e:
        logger.error(f"[RAWG] Error fetching metadata for '{game_name}': {e}")
        return {}
