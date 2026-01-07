"""VDF file utilities using the proven ValvePython vdf library"""

import sys
import os
from typing import Dict, Any

import vdf


def load_shortcuts_vdf(path: str) -> Dict[str, Any]:
    """Load and parse shortcuts.vdf file using vdf library"""
    try:
        with open(path, 'rb') as f:
            data = vdf.binary_loads(f.read())
        return data
    except FileNotFoundError:
        # Return empty structure if file doesn't exist
        return {"shortcuts": {}}
    except Exception as e:
        print(f"Error loading shortcuts.vdf: {e}")
        return {"shortcuts": {}}


def save_shortcuts_vdf(path: str, data: Dict[str, Any]) -> bool:
    """Save data to shortcuts.vdf file using vdf library"""
    try:
        # Create backup
        if os.path.exists(path):
            backup_path = path + '.backup'
            with open(path, 'rb') as f_in:
                with open(backup_path, 'wb') as f_out:
                    f_out.write(f_in.read())

        # Write with vdf library
        with open(path, 'wb') as f:
            binary_data = vdf.binary_dumps(data)
            f.write(binary_data)
            f.flush()
            os.fsync(f.fileno())  # Force write to disk

        # Validate write
        try:
            validation_data = load_shortcuts_vdf(path)
            expected_count = len(data.get('shortcuts', {}))
            actual_count = len(validation_data.get('shortcuts', {}))

            if actual_count != expected_count:
                print(f"ERROR: Write validation failed! Expected {expected_count}, got {actual_count}")
                # Restore backup
                if os.path.exists(backup_path):
                    import shutil
                    shutil.copy(backup_path, path)
                return False

            print(f"✓ Write validated: {actual_count} shortcuts persisted to disk")
        except Exception as e:
            print(f"Warning: Could not validate write: {e}")

        return True
    except Exception as e:
        print(f"Error saving shortcuts.vdf: {e}")
        import traceback
        traceback.print_exc()
        return False
