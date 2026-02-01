#!/usr/bin/env python3
"""
Cleanup script to remove broken Unifideck shortcuts from previous versions.

Use this if you have shortcuts from v39/v40 that are missing app IDs.
These shortcuts won't display artwork because they don't have the appid field.

Usage:
  1. Close Steam completely: killall steam
  2. Run this script: python3 cleanup_broken_shortcuts.py
  3. Start Steam: steam &
  4. Sync libraries in QAM → Unifideck → Sync Libraries

The script detects Unifideck games by their LaunchOptions (epic:xxx or gog:xxx)
and removes them while preserving your original non-steam games.
"""

import os
import sys
import shutil
from pathlib import Path

# Add parent directory to path to import vdf_utils
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, parent_dir)

from backend.utils.vdf import load_shortcuts_vdf, save_shortcuts_vdf

def find_active_user() -> tuple:
    """Find the most recently active Steam user"""
    userdata_path = Path.home() / '.steam/steam/userdata'

    if not userdata_path.exists():
        return None, None

    user_dirs = []
    for d in userdata_path.iterdir():
        if d.is_dir() and d.name.isdigit():
            user_dirs.append((d.name, d.stat().st_mtime))

    if not user_dirs:
        return None, None

    # Sort by most recent
    user_dirs.sort(key=lambda x: x[1], reverse=True)
    user_id = user_dirs[0][0]
    shortcuts_path = userdata_path / user_id / 'config' / 'shortcuts.vdf'

    return user_id, shortcuts_path

def main():
    print("=" * 80)
    print("Unifideck Shortcuts Cleanup Tool")
    print("=" * 80)

    # Find active user
    user_id, shortcuts_path = find_active_user()

    if not user_id:
        print("ERROR: No Steam user directories found!")
        return 1

    print(f"Active Steam User: {user_id}")
    print(f"Shortcuts file: {shortcuts_path}")

    if not shortcuts_path.exists():
        print(f"ERROR: shortcuts.vdf not found")
        return 1

    # Backup
    backup_path = shortcuts_path.with_suffix('.vdf.backup-' + str(int(Path.home().stat().st_mtime)))
    shutil.copy(shortcuts_path, backup_path)
    print(f"\n✓ Backup created: {backup_path}")

    # Load shortcuts
    shortcuts = load_shortcuts_vdf(str(shortcuts_path))
    total_before = len(shortcuts.get('shortcuts', {}))
    print(f"\n  Total shortcuts before cleanup: {total_before}")

    # Detect Unifideck games by LaunchOptions pattern (epic:xxx or gog:xxx)
    unifideck_shortcuts = []
    original_shortcuts = {}

    for idx, shortcut in shortcuts.get('shortcuts', {}).items():
        launch_opts = shortcut.get('LaunchOptions', '')

        # Unifideck games have LaunchOptions like "epic:xxx" or "gog:xxx"
        if launch_opts.startswith('epic:') or launch_opts.startswith('gog:'):
            unifideck_shortcuts.append((idx, shortcut.get('AppName', 'Unknown'), launch_opts))
        else:
            # Keep original non-steam games
            original_shortcuts[idx] = shortcut

    print(f"  Unifideck games found: {len(unifideck_shortcuts)}")
    print(f"  Original non-steam games: {len(original_shortcuts)}")

    if len(unifideck_shortcuts) == 0:
        print("\n✓ No Unifideck shortcuts to remove!")
        return 0

    # Show what will be removed
    print(f"\n  Will remove {len(unifideck_shortcuts)} Unifideck games:")
    for idx, name, launch in unifideck_shortcuts[:5]:
        store = 'EPIC' if launch.startswith('epic:') else 'GOG'
        print(f"    - [{store}] {name}")
    if len(unifideck_shortcuts) > 5:
        print(f"    ... and {len(unifideck_shortcuts) - 5} more")

    # Show what will be kept
    if original_shortcuts:
        print(f"\n  Will keep {len(original_shortcuts)} original games:")
        for idx, shortcut in list(original_shortcuts.items())[:3]:
            print(f"    - {shortcut.get('AppName', 'Unknown')}")
        if len(original_shortcuts) > 3:
            print(f"    ... and {len(original_shortcuts) - 3} more")

    # Rebuild shortcuts with only originals (re-index from 0)
    clean_shortcuts = {"shortcuts": {}}
    for new_idx, (old_idx, shortcut) in enumerate(original_shortcuts.items()):
        clean_shortcuts["shortcuts"][str(new_idx)] = shortcut

    # Write clean version
    success = save_shortcuts_vdf(str(shortcuts_path), clean_shortcuts)

    if success:
        print(f"\n✓ SUCCESS!")
        print(f"  Removed: {len(unifideck_shortcuts)} Unifideck games")
        print(f"  Kept: {len(original_shortcuts)} original non-steam games")
        print("\n" + "=" * 80)
        print("NEXT STEPS:")
        print("=" * 80)
        print("1. Start Steam:")
        print("   steam &")
        print()
        print("2. Once Steam is running, sync libraries:")
        print("   QAM → Unifideck → Sync Libraries")
        print()
        print("3. After sync completes, restart Steam to see artwork:")
        print("   killall steam && sleep 5 && steam &")
        print("=" * 80)
        return 0
    else:
        print(f"\n✗ ERROR: Failed to write shortcuts.vdf")
        print(f"  You can restore from: {backup_path}")
        return 1

if __name__ == '__main__':
    sys.exit(main())
