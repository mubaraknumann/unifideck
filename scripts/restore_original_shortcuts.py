#!/usr/bin/env python3
"""
Restore original 39 non-Unifideck shortcuts that were lost during v43 sync.

This script merges the 39 original shortcuts from the backup file with the
current 315 Unifideck shortcuts to create a complete shortcuts.vdf with 354 total.

The 39 originals include:
- Xbox Cloud Gaming shortcuts (--xcloud=GAMENAME)
- Heroic Games Launcher (Flatpak)
- Other third-party launchers

Usage:
    python3 scripts/restore_original_shortcuts.py
"""

import os
import sys
from pathlib import Path

# Add lib to path for vdf_utils
sys.path.append('/home/deck/homebrew/plugins/unifideck-decky')
from vdf_utils import load_shortcuts_vdf, save_shortcuts_vdf


def main():
    user_id = '225630054'
    backup_path = Path.home() / f'.steam/steam/userdata/{user_id}/config/shortcuts.vdf.before-v41-cleanup'
    current_path = Path.home() / f'.steam/steam/userdata/{user_id}/config/shortcuts.vdf'

    print(f"Loading backup from: {backup_path}")
    backup = load_shortcuts_vdf(str(backup_path))

    print(f"Loading current shortcuts from: {current_path}")
    current = load_shortcuts_vdf(str(current_path))

    # Extract 39 non-Unifideck shortcuts from backup
    originals = {}
    for idx, s in backup['shortcuts'].items():
        launch = s.get('LaunchOptions', '')
        if not (launch.startswith('epic:') or launch.startswith('gog:')):
            originals[idx] = s

    print(f"Found {len(originals)} original non-Unifideck shortcuts in backup")

    # Extract current Unifideck shortcuts
    unifideck = {}
    for idx, s in current['shortcuts'].items():
        launch = s.get('LaunchOptions', '')
        if launch.startswith('epic:') or launch.startswith('gog:'):
            unifideck[idx] = s

    print(f"Found {len(unifideck)} Unifideck shortcuts in current file")

    # Merge: originals first (indices 0-38), then unifideck (indices 39+)
    merged = {"shortcuts": {}}
    next_idx = 0

    # Add originals
    print(f"\nMerging shortcuts...")
    for _, shortcut in originals.items():
        merged["shortcuts"][str(next_idx)] = shortcut
        print(f"  [{next_idx}] {shortcut.get('AppName', 'Unknown')}")
        next_idx += 1

    print(f"\nAdding Unifideck shortcuts starting at index {next_idx}...")
    # Add unifideck
    for _, shortcut in unifideck.items():
        merged["shortcuts"][str(next_idx)] = shortcut
        next_idx += 1

    # Save merged shortcuts
    print(f"\nSaving merged shortcuts to: {current_path}")
    success = save_shortcuts_vdf(str(current_path), merged)

    if success:
        print(f"\n✅ Restored {len(originals)} original + {len(unifideck)} Unifideck = {next_idx} total shortcuts")
        print(f"\nYou can now:")
        print(f"  1. Restart Steam to see all shortcuts")
        print(f"  2. Install v44 from Decky QAM when ready")
        print(f"  3. Sync libraries - the smart update will preserve your 39 originals")
    else:
        print(f"\n❌ Failed to save merged shortcuts")
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
