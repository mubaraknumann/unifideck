#!/usr/bin/env python3
"""
Amazon Language Setter - Sets Windows locale in Wine prefix for Amazon games.

Usage: python3 amazon_set_language.py <prefix_path>

Since Nile CLI has no --language flag, this script sets the Windows locale
in the Wine prefix registry so that games detect the correct language.

It modifies HKEY_CURRENT_USER\Control Panel\International in user.reg.
"""

import json
import os
import re
import sys

# Mapping from Unifideck language codes to Windows locale data
# Format: { code: (LCID_hex, sLanguage_3letter, LocaleName, sCountry) }
LOCALE_MAP = {
    'en-US': ('00000409', 'ENU', 'en-US', 'United States'),
    'de-DE': ('00000407', 'DEU', 'de-DE', 'Germany'),
    'fr-FR': ('0000040c', 'FRA', 'fr-FR', 'France'),
    'es-ES': ('00000c0a', 'ESN', 'es-ES', 'Spain'),
    'it-IT': ('00000410', 'ITA', 'it-IT', 'Italy'),
    'pt-BR': ('00000416', 'PTB', 'pt-BR', 'Brazil'),
    'ru-RU': ('00000419', 'RUS', 'ru-RU', 'Russia'),
    'pl-PL': ('00000415', 'PLK', 'pl-PL', 'Poland'),
    'zh-CN': ('00000804', 'CHS', 'zh-CN', 'China'),
    'ja-JP': ('00000411', 'JPN', 'ja-JP', 'Japan'),
    'ko-KR': ('00000412', 'KOR', 'ko-KR', 'Korea'),
    'nl-NL': ('00000413', 'NLD', 'nl-NL', 'Netherlands'),
    'tr-TR': ('0000041f', 'TRK', 'tr-TR', 'Turkey'),
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


def smart_match_locale(target: str) -> tuple | None:
    """Find best locale match for the target language code."""
    if not target:
        return None

    # Exact match
    if target in LOCALE_MAP:
        return LOCALE_MAP[target]

    # Base language match (e.g., 'en' matches 'en-US', 'de' matches 'de-DE')
    target_base = target.split('-')[0].lower()
    for code, data in LOCALE_MAP.items():
        if code.split('-')[0].lower() == target_base:
            return data

    return None


def update_user_reg(prefix_path: str, lcid: str, slanguage: str, locale_name: str, scountry: str):
    """Update the Wine prefix user.reg to set Windows locale."""
    user_reg = os.path.join(prefix_path, 'user.reg')

    if not os.path.exists(user_reg):
        print(f"user.reg not found at {user_reg} - prefix may not be initialized yet")
        return False

    with open(user_reg, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    # The section we need to modify
    section_header = '[Control Panel\\\\International]'

    # Registry values to set
    new_values = {
        'Locale': lcid,
        'LocaleName': locale_name,
        'sLanguage': slanguage,
        'sCountry': scountry,
    }

    if section_header in content:
        # Section exists - update values within it
        # Find the section boundaries
        section_start = content.index(section_header)
        # Find the next section (starts with \n[ at the beginning of a line)
        next_section = re.search(r'\n\[', content[section_start + len(section_header):])
        if next_section:
            section_end = section_start + len(section_header) + next_section.start()
        else:
            section_end = len(content)

        section_body = content[section_start + len(section_header):section_end]

        for key, value in new_values.items():
            # Wine registry format: "key"="value"
            pattern = rf'^"{re.escape(key)}"="[^"]*"'
            replacement = f'"{key}"="{value}"'
            new_body, count = re.subn(pattern, replacement, section_body, flags=re.MULTILINE)
            if count > 0:
                section_body = new_body
                print(f"  Updated {key}={value}")
            else:
                # Key doesn't exist, add it
                section_body = section_body.rstrip('\n') + f'\n"{key}"="{value}"\n'
                print(f"  Added {key}={value}")

        content = content[:section_start + len(section_header)] + section_body + content[section_end:]
    else:
        # Section doesn't exist - append it
        print(f"  Creating {section_header} section")
        new_section = f'\n{section_header}\n'
        for key, value in new_values.items():
            new_section += f'"{key}"="{value}"\n'
            print(f"  Added {key}={value}")
        content += new_section

    with open(user_reg, 'w', encoding='utf-8') as f:
        f.write(content)

    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: amazon_set_language.py <prefix_path>")
        sys.exit(1)

    prefix_path = sys.argv[1]
    print(f"Amazon Language Setter")
    print(f"Prefix: {prefix_path}")

    # Get user's preferred language
    preferred_lang = get_unifideck_language()
    print(f"User preferred language: {preferred_lang}")

    # Find matching locale data
    locale_data = smart_match_locale(preferred_lang)
    if not locale_data:
        print(f"No locale mapping for {preferred_lang}, defaulting to en-US")
        locale_data = LOCALE_MAP['en-US']

    lcid, slanguage, locale_name, scountry = locale_data
    print(f"Setting Windows locale: {locale_name} (LCID={lcid}, sLanguage={slanguage})")

    # Update the Wine prefix registry
    if update_user_reg(prefix_path, lcid, slanguage, locale_name, scountry):
        print("Windows locale updated successfully!")
    else:
        print("Could not update Windows locale (prefix may not exist yet)")


if __name__ == '__main__':
    main()
