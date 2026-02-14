import os

def get_steam_path() -> str:
    """Get Steam installation path"""
    # Try common locations
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".steam", "steam"),
        os.path.join(home, ".local", "share", "Steam"),
        "/home/deck/.steam/steam",  # Steam Deck default
    ]

    for path in candidates:
        if os.path.exists(path):
            return path

    raise Exception("Steam path not found")


def create_cef_debugging_flag():
    """Create .cef-enable-remote-debugging flag in Steam folder"""
    try:
        steam_path = get_steam_path()
        flag_path = os.path.join(steam_path, ".cef-enable-remote-debugging")

        if not os.path.exists(flag_path):
            with open(flag_path, 'w') as f:
                pass  # Empty file
            print(f"[Unifideck CDP] Created CEF debugging flag at {flag_path}")
            print("[Unifideck CDP] Steam restart required for CDP to work")
            return True  # Flag was created
        else:
            print(f"[Unifideck CDP] CEF debugging flag already exists")
            return False  # Flag already existed

    except Exception as e:
        print(f"[Unifideck CDP] Failed to create CEF flag: {e}")
        return False
