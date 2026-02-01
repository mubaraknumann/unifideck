"""Steam Deck compatibility and verification utilities."""

import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


# Steam Deck test result tokens -> Human-readable descriptions
DECK_TEST_RESULT_TOKENS = {
    '#SteamDeckVerified_TestResult_DefaultControllerConfigFullyFunctional': 
        'All functionality is accessible when using the default controller configuration',
    '#SteamDeckVerified_TestResult_ControllerGlyphsMatchDeckDevice': 
        'This game shows Steam Deck controller icons',
    '#SteamDeckVerified_TestResult_InterfaceTextIsLegible': 
        'In-game interface text is legible on Steam Deck',
    '#SteamDeckVerified_TestResult_DefaultConfigurationIsPerformant': 
        "This game's default graphics configuration performs well on Steam Deck",
    '#SteamDeckVerified_TestResult_LauncherInteractionIssues': 
        "This game's launcher/setup tool may require the touchscreen or virtual keyboard, or have difficult to read text",
    '#SteamDeckVerified_TestResult_NativeResolutionNotDefault': 
        "This game supports Steam Deck's native display resolution but does not set it by default and may require you to configure the display resolution manually",
    '#SteamDeckVerified_TestResult_ControllerGlyphsDoNotMatchDeckDevice': 
        'This game sometimes shows non-Steam-Deck controller icons',
    '#SteamDeckVerified_TestResult_ExternalControllersNotSupportedLocalMultiplayer': 
        'This game does not default to external Bluetooth/USB controllers on Deck, and may require manually switching the active controller via the Quick Access Menu',
    '#SteamOS_TestResult_GameStartupFunctional': 
        'This game runs successfully on SteamOS',
}


async def fetch_steam_deck_compatibility(steam_app_id: int) -> Dict[str, Any]:
    """Fetch Steam Deck compatibility rating from Steam API.
    
    Args:
        steam_app_id: Steam App ID to check
        
    Returns:
        Dict with:
            'category': int (0=Unknown, 1=Unsupported, 2=Playable, 3=Verified)
            'testResults': List of dicts with 'text' and 'passed' keys
    """
    if not steam_app_id:
        return {'category': 0, 'testResults': []}
    
    try:
        import aiohttp
        url = f"https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport?nAppID={steam_app_id}"
        
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    logger.warning(f"[DeckCompat] API returned status {response.status} for app {steam_app_id}")
                    return {'category': 0, 'testResults': []}
                
                data = await response.json()
        
        # Handle edge case where API returns a list instead of dict
        if not isinstance(data, dict):
            logger.debug(f"[DeckCompat] Unexpected response type for app {steam_app_id}: {type(data).__name__}")
            return {'category': 0, 'testResults': []}
                
        if not data.get('success'):
            return {'category': 0, 'testResults': []}
        
        results = data.get('results', {})
        if not isinstance(results, dict):
            logger.debug(f"[DeckCompat] 'results' is {type(results).__name__}, not dict, for app {steam_app_id}")
            return {'category': 0, 'testResults': []}
        
        category = results.get('resolved_category', 0)
        
        # Convert test result tokens to human-readable strings
        test_results = []
        for item in results.get('resolved_items', []):
            token = item.get('loc_token', '')
            display_type = item.get('display_type', 0)  # 4=pass, 3=warning
            
            # Look up token in our mapping, or fall back to cleaned token
            text = DECK_TEST_RESULT_TOKENS.get(
                token, 
                token.replace('#SteamDeckVerified_TestResult_', '')
                     .replace('#SteamOS_TestResult_', '')
            )
            
            if text:
                test_results.append({
                    'text': text,
                    'passed': display_type == 4  # 4 = checkmark, 3 = warning
                })
        
        logger.info(f"[DeckCompat] App {steam_app_id}: category={category}, {len(test_results)} test results")
        return {'category': category, 'testResults': test_results}

    except Exception as e:
        logger.warning(f"[DeckCompat] Failed to fetch for app {steam_app_id}: {e}")
        return {'category': 0, 'testResults': []}
