#!/usr/bin/env python3
"""
GOG Language Setter - Modifies goggame-*.info to set game language at launch time.

Usage: python3 gog_set_language.py <game_id> <install_dir>

This script:
1. Reads the user's preferred language from Unifideck settings
2. Finds the goggame-*.info file in the install directory
3. Modifies the 'language' and 'languages' fields to match preference
4. The game will then launch in the correct language

This is a LAUNCH-TIME fix that works even if the download set the wrong language.
"""

import json
import os
import sys
import glob

# Language code to display name mapping
LANG_DISPLAY_NAMES = {
    'en-US': 'English',
    'fr-FR': 'French',
    'de-DE': 'German',
    'es-ES': 'Spanish',
    'it-IT': 'Italian',
    'pt-BR': 'Portuguese (Brazil)',
    'ru-RU': 'Russian',
    'pl-PL': 'Polish',
    'zh-CN': 'Chinese (Simplified)',
    'zh-Hans': 'Chinese (Simplified)',
    'zh-TW': 'Chinese (Traditional)',
    'zh-Hant': 'Chinese (Traditional)',
    'ja-JP': 'Japanese',
    'ko-KR': 'Korean',
    'nl-NL': 'Dutch',
    'tr-TR': 'Turkish',
}

def get_unifideck_language() -> str:
    """Get the user's preferred language from Unifideck settings."""
    settings_path = os.path.expanduser("~/.local/share/unifideck/settings.json")
    try:
        if os.path.exists(settings_path):
            with open(settings_path, 'r') as f:
                settings = json.load(f)
                lang = settings.get('language', 'en-US')
                if lang and lang != 'auto':
                    return lang
    except Exception as e:
        print(f"Could not read settings: {e}")
    return 'en-US'

def smart_match_language(target: str, available: list) -> str | None:
    """Find best match for target language in available list."""
    if not target or not available:
        return None
    
    # Exact match
    if target in available:
        return target
    
    # Base language match (e.g., 'en-US' matches 'en')
    target_base = target.split('-')[0].lower()
    for lang in available:
        lang_base = lang.split('-')[0].lower()
        if target_base == lang_base:
            return lang
    
    return None

def main():
    if len(sys.argv) < 3:
        print("Usage: gog_set_language.py <game_id> <install_dir>")
        sys.exit(1)
    
    game_id = sys.argv[1]
    install_dir = sys.argv[2]
    
    print(f"GOG Language Setter for game {game_id}")
    print(f"Install directory: {install_dir}")
    
    # Get user's preferred language
    preferred_lang = get_unifideck_language()
    print(f"User preferred language: {preferred_lang}")
    
    # Find goggame-*.info file - it might be in a parent dir (if WORK_DIR is Binaries/...)
    info_pattern = None
    search_dir = install_dir
    
    # Search up to 4 levels up
    for _ in range(4):
        candidate = os.path.join(search_dir, f"goggame-{game_id}.info")
        if os.path.exists(candidate):
            info_pattern = candidate
            print(f"Found info file at: {info_pattern}")
            break
            
        # Try glob pattern
        candidates = glob.glob(os.path.join(search_dir, f"goggame-*.info"))
        if candidates:
            # Filter to ensure it looks like a gog info file (sometimes there are backups)
            for c in candidates:
                if os.path.basename(c).startswith(f"goggame-{game_id}"):
                    info_pattern = c
                    print(f"Found info file via glob at: {info_pattern}")
                    break
            if info_pattern:
                break
        
        # Move up one level
        parent = os.path.dirname(search_dir)
        if parent == search_dir: # Reached root
            break
        search_dir = parent

    if not info_pattern:
        print(f"No goggame info file found starting from {install_dir} (searched 4 levels up)")
        sys.exit(0) # Not an error, just skip
    
    # Read current info
    try:
        with open(info_pattern, 'r', encoding='utf-8') as f:
            info = json.load(f)
    except Exception as e:
        print(f"Error reading info file: {e}")
        sys.exit(1)
    
    current_lang = info.get('language', 'Unknown')
    print(f"Current game language: {current_lang}")
    
    # Check if game supports multiple languages
    # The 'languages' field lists installed language packs
    installed_langs = info.get('languages', [])
    print(f"Installed language packs: {installed_langs}")
    
    # Try to find available languages from depots or manifest
    # For now, we'll check if the preferred language is in the typical list
    # and modify regardless (the game will use it if files exist)
    
    # Smart match to find closest available language
    matched_lang = smart_match_language(preferred_lang, installed_langs)
    
    if not matched_lang:
        # If no match in installed, check if we can deduce available from file structure
        # For now, just try to set it anyway - the game files might support it
        print(f"Preferred language {preferred_lang} not in installed list, but files may exist")
        matched_lang = preferred_lang
    
    # Get display name
    display_name = LANG_DISPLAY_NAMES.get(matched_lang, matched_lang)
    
    if matched_lang and matched_lang != installed_langs[0] if installed_langs else True:
        print(f"Setting language to: {matched_lang} ({display_name})")
        
        # Update the info file
        info['language'] = display_name
        info['languages'] = [matched_lang]
        
        try:
            with open(info_pattern, 'w', encoding='utf-8') as f:
                json.dump(info, f, indent=2)
            print("Language updated successfully!")
        except Exception as e:
            print(f"Error writing info file: {e}")
            sys.exit(1)
    else:
        print(f"Language already set correctly or no change needed")

if __name__ == '__main__':
    main()
