#!/usr/bin/env python3
"""
Helper script to resolve game language preference.
Used by unifideck-launcher to determine which language to use for a game.

Usage: python3 resolve_language.py <store> <game_id>
Outputs: Language code (e.g., "en-US", "pt-BR")
"""

import sys
import os

# Add parent directory to path to import backend modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.utils.language import get_resolved_language


def main():
    if len(sys.argv) != 3:
        print("en-US", file=sys.stderr)  # Fallback to English
        sys.exit(1)

    store = sys.argv[1]
    game_id = sys.argv[2]

    try:
        language = get_resolved_language(store, game_id)
        print(language)
        sys.exit(0)
    except Exception as e:
        print(f"Error resolving language: {e}", file=sys.stderr)
        print("en-US", file=sys.stderr)  # Fallback to English
        sys.exit(1)


if __name__ == "__main__":
    main()
