"""
Language Resolution Utility for Unifideck

Provides centralized language preference resolution with three-tier fallback:
1. Per-game language override (highest priority)
2. Global Unifideck language preference
3. System locale detection (fallback)

This ensures consistent language handling across downloads, launcher, and game execution.
"""

import os
import json
import locale
import logging
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

# Settings file path
SETTINGS_PATH = os.path.expanduser("~/.local/share/unifideck/settings.json")

# Supported languages mapping (2-letter code -> full locale code)
# This list covers major languages supported by Epic, GOG, and most game stores
LANGUAGE_MAP = {
    'en': 'en-US',
    'pt': 'pt-BR',
    'es': 'es-ES',
    'fr': 'fr-FR',
    'de': 'de-DE',
    'it': 'it-IT',
    'ru': 'ru-RU',
    'pl': 'pl-PL',
    'ja': 'ja-JP',
    'ko': 'ko-KR',
    'zh': 'zh-CN',
    'nl': 'nl-NL',
    'tr': 'tr-TR',
    'uk': 'uk-UA',
    'ar': 'ar-SA',
    'cs': 'cs-CZ',
    'da': 'da-DK',
    'fi': 'fi-FI',
    'el': 'el-GR',
    'hu': 'hu-HU',
    'no': 'no-NO',
    'sv': 'sv-SE',
    'th': 'th-TH',
}

# Supported languages for different stores
# Epic and Amazon use 2-letter codes, GOG uses full locale codes
EPIC_SUPPORTED_LANGUAGES = [
    'en', 'ar', 'de', 'es', 'es-MX', 'fr', 'it', 'ja', 'ko', 
    'pl', 'pt-BR', 'ru', 'th', 'tr', 'zh-Hans', 'zh-Hant'
]

GOG_SUPPORTED_LANGUAGES = [
    'en-US', 'de-DE', 'fr-FR', 'pl-PL', 'ru-RU', 'pt-BR', 
    'es-ES', 'it-IT', 'zh-CN', 'ko-KR', 'ja-JP', 'nl-NL', 'tr-TR'
]

# Amazon typically follows system locale, but we provide this list for UI
AMAZON_SUPPORTED_LANGUAGES = [
    'en-US', 'de-DE', 'es-ES', 'fr-FR', 'it-IT', 'ja-JP', 
    'pt-BR', 'zh-CN'
]


def _load_settings() -> Dict:
    """Load Unifideck settings from JSON file."""
    try:
        if os.path.exists(SETTINGS_PATH):
            with open(SETTINGS_PATH, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.debug(f"[Language] Could not load settings: {e}")
    return {}


def _save_settings(settings: Dict) -> bool:
    """Save Unifideck settings to JSON file."""
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, 'w') as f:
            json.dump(settings, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"[Language] Error saving settings: {e}")
        return False


def _detect_system_locale() -> str:
    """Detect system locale and map to full locale code.
    
    Returns:
        Full locale code (e.g., 'pt-BR', 'en-US')
    """
    try:
        lang_tuple = locale.getlocale()
        if lang_tuple and lang_tuple[0]:
            # Extract 2-letter code: 'en_US' -> 'en', 'de_DE' -> 'de'
            lang_code = lang_tuple[0].split('_')[0].lower()
            
            # Map to full locale code
            full_locale = LANGUAGE_MAP.get(lang_code, 'en-US')
            logger.debug(f"[Language] Detected system locale: {lang_code} -> {full_locale}")
            return full_locale
    except Exception as e:
        logger.debug(f"[Language] Could not detect system locale: {e}")
    
    # Default fallback
    return 'en-US'


def get_resolved_language(store: str, game_id: str) -> str:
    """Resolve language for a specific game with three-tier fallback.
    
    Priority:
    1. Per-game language override (if set)
    2. Global Unifideck language preference (if not 'auto')
    3. System locale detection
    
    Args:
        store: Store name ('epic', 'gog', 'amazon')
        game_id: Game ID
    
    Returns:
        Full locale code (e.g., 'pt-BR', 'en-US')
    """
    settings = _load_settings()
    
    # Priority 1: Per-game override
    game_languages = settings.get('game_languages', {})
    game_key = f"{store}:{game_id}"
    if game_key in game_languages:
        lang = game_languages[game_key]
        if lang and lang != 'auto':
            logger.info(f"[Language] Using per-game override for {game_key}: {lang}")
            return lang
    
    # Priority 2: Global plugin preference
    global_lang = settings.get('language', 'auto')
    if global_lang and global_lang != 'auto':
        logger.info(f"[Language] Using global preference for {game_key}: {global_lang}")
        return global_lang
    
    # Priority 3: System locale
    system_lang = _detect_system_locale()
    logger.info(f"[Language] Using system locale for {game_key}: {system_lang}")
    return system_lang


def get_game_language_preference(store: str, game_id: str) -> str:
    """Get the language preference for a specific game.
    
    Args:
        store: Store name ('epic', 'gog', 'amazon')
        game_id: Game ID
    
    Returns:
        Language code or 'auto' if not set
    """
    settings = _load_settings()
    game_languages = settings.get('game_languages', {})
    game_key = f"{store}:{game_id}"
    return game_languages.get(game_key, 'auto')


def set_game_language_preference(store: str, game_id: str, language: str) -> bool:
    """Set the language preference for a specific game.
    
    Args:
        store: Store name ('epic', 'gog', 'amazon')
        game_id: Game ID
        language: Language code (e.g., 'pt-BR') or 'auto' to clear override
    
    Returns:
        True if saved successfully
    """
    settings = _load_settings()
    
    # Initialize game_languages if not present
    if 'game_languages' not in settings:
        settings['game_languages'] = {}
    
    game_key = f"{store}:{game_id}"
    
    # If 'auto', remove the override (let it fall back to global/system)
    if language == 'auto':
        if game_key in settings['game_languages']:
            del settings['game_languages'][game_key]
            logger.info(f"[Language] Cleared per-game override for {game_key}")
    else:
        settings['game_languages'][game_key] = language
        logger.info(f"[Language] Set per-game language for {game_key}: {language}")
    
    return _save_settings(settings)


def get_supported_languages(store: str) -> List[str]:
    """Get list of supported languages for a store.
    
    Args:
        store: Store name ('epic', 'gog', 'amazon')
    
    Returns:
        List of language codes supported by the store
    """
    if store == 'epic':
        return EPIC_SUPPORTED_LANGUAGES
    elif store == 'gog':
        return GOG_SUPPORTED_LANGUAGES
    elif store == 'amazon':
        return AMAZON_SUPPORTED_LANGUAGES
    else:
        # Fallback: return common languages
        return ['en-US', 'pt-BR', 'es-ES', 'fr-FR', 'de-DE', 'it-IT', 'ru-RU', 'ja-JP', 'zh-CN']


def normalize_language_code(lang_code: str, target_format: str = 'full') -> str:
    """Normalize language code between different formats.
    
    Args:
        lang_code: Input language code (e.g., 'en', 'en-US', 'en_US')
        target_format: Target format ('full' for 'en-US', 'short' for 'en')
    
    Returns:
        Normalized language code
    """
    if not lang_code or lang_code == 'auto':
        return lang_code
    
    # Replace underscore with hyphen
    lang_code = lang_code.replace('_', '-')
    
    if target_format == 'short':
        # Extract 2-letter code
        return lang_code.split('-')[0].lower()
    else:  # target_format == 'full'
        # If already full format, return as-is
        if '-' in lang_code:
            return lang_code
        # Map 2-letter to full
        return LANGUAGE_MAP.get(lang_code.lower(), 'en-US')
