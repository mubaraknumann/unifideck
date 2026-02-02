"""Metacritic backend API scraper adapter.

Uses the Metacritic backend composer API to fetch game metadata.
Reference: https://backend.metacritic.com/composer/metacritic/pages/games/{slug}/web
"""
import asyncio
import logging
import re
import ssl
import unicodedata
import certifi
from typing import Dict, List, Any, Optional
from datetime import datetime

try:
    import aiohttp
except ImportError:
    aiohttp = None

logger = logging.getLogger(__name__)

# Metacritic backend API configuration
METACRITIC_API_BASE = "https://backend.metacritic.com"
METACRITIC_API_KEY = "1MOZgmNFxvmljaQR1X9KAij9Mo4xAY3u"
METACRITIC_PRODUCT_TYPE = "games"


def slugify_game_name(name: str) -> str:
    """Convert a game title to a URL-friendly slug for Metacritic URLs.
    
    Examples:
        "The Legend of Zelda: Ocarina of Time" -> "the-legend-of-zelda-ocarina-of-time"
        "Hades" -> "hades"
        "Portal 2" -> "portal-2"
    """
    # Normalize to ASCII and lowercase
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('utf-8').lower()
    # Replace '+' with 'plus'
    name = name.replace('+', 'plus')
    # Remove all non-alphanumeric characters except spaces and hyphens
    name = re.sub(r'[^a-z0-9 \-]+', '', name)
    # Replace spaces with hyphens
    name = re.sub(r'\s+', '-', name)
    # Remove consecutive hyphens
    name = re.sub(r'-+', '-', name)
    return name.strip('-')


def clean_title(title: str) -> str:
    """Clean title for better matching - removes trademark symbols."""
    title = re.sub(r'[\u2122\u00AE]', '', title)  # Remove ™ and ®
    return title.strip()


def strip_suffixes(title: str) -> str:
    """Remove common game edition suffixes for better matching."""
    suffixes = [
        r":?\s*Director's Cut",
        r":?\s*Game of the Year Edition",
        r":?\s*GOTY Edition",
        r":?\s*Remastered",
        r":?\s*Definitive Edition",
        r":?\s*Bonus Edition",
        r":?\s*Deluxe Edition",
        r":?\s*Special Edition",
        r":?\s*Anniversary Edition",
        r":?\s*Complete Edition",
        r":?\s*Ultimate Edition",
        r":?\s*Gold Edition",
        r":?\s*Enhanced Edition",
    ]
    cleaned = title
    for suffix in suffixes:
        cleaned = re.sub(suffix, "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def to_roman(num_str: str) -> Optional[str]:
    """Convert Arabic numeral string (1-5) to Roman numeral."""
    mapping = {'1': 'I', '2': 'II', '3': 'III', '4': 'IV', '5': 'V'}
    return mapping.get(num_str)


def to_arabic(roman_str: str) -> Optional[str]:
    """Convert Roman numeral (I-V) to Arabic numeral string."""
    mapping = {'I': '1', 'II': '2', 'III': '3', 'IV': '4', 'V': '5'}
    return mapping.get(roman_str)


def get_numeral_variants(title: str) -> List[str]:
    """Get title variants with swapped Arabic/Roman numerals.
    
    Examples:
        "Cat Quest II" -> ["Cat Quest 2"]
        "Portal 2" -> ["Portal II"]
    """
    candidates = []
    
    # Swap trailing Romans (Cat Quest II -> Cat Quest 2)
    match_roman_end = re.search(r'\b(I|II|III|IV|V)$', title)
    if match_roman_end:
        roman = match_roman_end.group(1)
        arabic = to_arabic(roman)
        if arabic:
            candidates.append(title[:match_roman_end.start()] + arabic)
    
    # Swap trailing Arabic (Portal 2 -> Portal II)
    match_arabic_end = re.search(r'\b([1-5])$', title)
    if match_arabic_end:
        num = match_arabic_end.group(1)
        roman = to_roman(num)
        if roman:
            candidates.append(title[:match_arabic_end.start()] + roman)
    
    # Swap any numeral in the middle
    def replace_arabic(match):
        return to_roman(match.group(1)) or match.group(0)
    
    def replace_roman(match):
        return to_arabic(match.group(1)) or match.group(0)
    
    subbed_roman = re.sub(r'\b([1-5])\b', replace_arabic, title)
    if subbed_roman != title:
        candidates.append(subbed_roman)
    
    subbed_arabic = re.sub(r'\b(I|II|III|IV|V)\b', replace_roman, title)
    if subbed_arabic != title:
        candidates.append(subbed_arabic)
    
    return list(set(candidates))


class MetacriticScraper:
    """Adapter for Metacritic backend composer API."""
    
    def __init__(self, timeout: float = 10.0, backoff_factor: float = 0.5):
        """Initialize scraper with timeout and backoff settings."""
        self.timeout = timeout
        self.backoff_factor = backoff_factor
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if aiohttp is None:
            raise ImportError("aiohttp is required for Metacritic fetching")
        
        if self.session is None or self.session.closed:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_context, limit_per_host=5)
            self.session = aiohttp.ClientSession(
                connector=connector,
                headers={"User-Agent": "Mozilla/5.0"}
            )
        
        return self.session
    
    async def _fetch_product_page(self, slug: str, max_retries: int = 2) -> Optional[Dict[str, Any]]:
        """Fetch product page from composer API.
        
        Uses: /composer/metacritic/pages/games/{slug}/web
        """
        url = f"{METACRITIC_API_BASE}/composer/metacritic/pages/{METACRITIC_PRODUCT_TYPE}/{slug}/web"
        params = {
            'filter': 'all',
            'sort': 'date',
            'apiKey': METACRITIC_API_KEY
        }
        
        session = await self._get_session()
        
        for attempt in range(max_retries):
            try:
                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 404:
                        # Not found - don't retry
                        return None
                    elif resp.status == 429:
                        wait_time = self.backoff_factor * (2 ** attempt)
                        logger.debug(f"[Metacritic] Rate limited, backing off {wait_time}s")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        logger.debug(f"[Metacritic] Request failed for {slug}: {resp.status}")
                        return None
            
            except asyncio.TimeoutError:
                logger.debug(f"[Metacritic] Timeout for {slug} (attempt {attempt + 1})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(self.backoff_factor)
                continue
            
            except Exception as e:
                logger.debug(f"[Metacritic] Error for {slug}: {e}")
                return None
        
        return None
    
    async def fetch_game(self, game_title: str) -> Optional[Dict[str, Any]]:
        """Fetch game metadata from Metacritic using multiple matching strategies.
        
        Strategies:
        1. Direct slug match (cleaned title)
        2. Without edition suffixes
        3. Split by subtitle (first part)
        4. Numeral swapping (II <-> 2)
        """
        logger.debug(f"[Metacritic] Fetching metadata for: {game_title}")
        
        # Strategy 1: Direct cleaned match
        cleaned = clean_title(game_title)
        slug = slugify_game_name(cleaned)
        
        data = await self._fetch_product_page(slug)
        if data:
            return self._extract_metadata(data, "exact_match")
        
        # Strategy 2: Without suffixes
        no_suffix = strip_suffixes(cleaned)
        if no_suffix != cleaned:
            slug = slugify_game_name(no_suffix)
            data = await self._fetch_product_page(slug)
            if data:
                return self._extract_metadata(data, "suffix_removed")
        
        # Strategy 3: Split by subtitle
        for char in [':', '-', '–']:
            if char in cleaned:
                parts = cleaned.split(char)
                first_part = parts[0].strip()
                if len(first_part) > 2 and first_part != no_suffix:
                    slug = slugify_game_name(first_part)
                    data = await self._fetch_product_page(slug)
                    if data:
                        return self._extract_metadata(data, "split_subtitle")
        
        # Strategy 4: Numeral swapping
        for variant in get_numeral_variants(cleaned):
            slug = slugify_game_name(variant)
            data = await self._fetch_product_page(slug)
            if data:
                return self._extract_metadata(data, "numeral_swap")
        
        # Also try numeral swap on no_suffix version
        for variant in get_numeral_variants(no_suffix):
            slug = slugify_game_name(variant)
            data = await self._fetch_product_page(slug)
            if data:
                return self._extract_metadata(data, "numeral_swap_no_suffix")
        
        logger.debug(f"[Metacritic] No match found for '{game_title}'")
        return None
    
    def _extract_metadata(self, page_data: Dict[str, Any], match_method: str) -> Optional[Dict[str, Any]]:
        """Extract game metadata from composer page response."""
        try:
            components = page_data.get('components', [])
            if not components:
                return None
            
            # Component 0 has basic game info
            item_data = components[0].get('data', {}).get('item', {})
            if not item_data:
                return None
            
            # Component 6 typically has metascore for games
            metascore_data = {}
            userscore_data = {}
            
            # Find score components (index varies)
            for comp in components:
                comp_name = comp.get('meta', {}).get('componentName', '')
                data_item = comp.get('data', {}).get('item', {})
                
                if 'critic-score-summary' in comp_name and data_item:
                    metascore_data = data_item
                elif 'user-score-summary' in comp_name and data_item:
                    userscore_data = data_item
            
            # Extract scores
            metascore = metascore_data.get('score')
            userscore = userscore_data.get('score')
            
            # Convert userscore from 0-10 to 0-100
            if userscore is not None and 0 <= userscore <= 10:
                userscore = int(userscore * 10)
            
            # Extract genres
            genres = []
            for g in item_data.get('genres', []):
                if isinstance(g, dict) and g.get('name'):
                    genres.append(g['name'])
            
            # Extract platforms  
            platforms = []
            for p in item_data.get('platforms', []):
                if isinstance(p, dict) and p.get('name'):
                    platforms.append(p['name'])
            
            # Extract developer/publisher
            production = item_data.get('production', {})
            companies = production.get('companies', [])
            
            developer = ', '.join([
                c['name'] for c in companies
                if 'Developer' in c.get('typeName', '') and c.get('name')
            ])
            
            publisher = ', '.join([
                c['name'] for c in companies
                if 'Publisher' in c.get('typeName', '') and c.get('name')
            ])
            
            metadata = {
                'title': item_data.get('title'),
                'metascore': metascore,
                'userscore': userscore,
                'metascore_count': metascore_data.get('reviewCount'),
                'userscore_count': userscore_data.get('reviewCount'),
                'metascore_sentiment': metascore_data.get('sentiment'),
                'userscore_sentiment': userscore_data.get('sentiment'),
                'description': item_data.get('description', ''),
                'genres': genres,
                'platforms': platforms,
                'developer': developer,
                'publisher': publisher,
                'release_date': item_data.get('releaseDate'),
                'rating': item_data.get('rating'),
                'match_method': match_method,
            }
            
            logger.debug(f"[Metacritic] Found: {metadata.get('title')} (score: {metascore})")
            return metadata
            
        except Exception as e:
            logger.error(f"[Metacritic] Error extracting metadata: {e}")
            return None
    
    async def close(self):
        """Close the session."""
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


async def fetch_metacritic_metadata(
    game_title: str,
    timeout: float = 10.0,
    delay: float = 0.1
) -> Optional[Dict[str, Any]]:
    """Fetch Metacritic metadata for a single game.
    
    Args:
        game_title: Name of the game
        timeout: Request timeout in seconds
        delay: Delay in seconds before making request (for rate limiting)
        
    Returns:
        Metadata dict or None
    """
    if delay > 0:
        await asyncio.sleep(delay)
    
    scraper = MetacriticScraper(timeout=timeout)
    try:
        return await scraper.fetch_game(game_title)
    finally:
        await scraper.close()


def sanitize_metacritic_description(text: str, max_length: int = 1000) -> str:
    """Clean up Metacritic descriptions for display."""
    if not text:
        return ''
    
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Fix sentences joined without space
    text = re.sub(r'([.!?])([A-Z])', r'\1 \2', text)
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    
    if len(text) > max_length:
        text = text[:max_length].rsplit(' ', 1)[0] + '...'
    
    return text
