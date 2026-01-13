#!/usr/bin/env python3
import sys
import os
import json
import asyncio
import logging
import shutil
import tempfile
from unittest.mock import MagicMock, patch

# Add parent directory to path to import backend modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("GOGTest")

# Mock dependencies we don't need for the test
sys.modules['defaults.backend.auth.browser'] = MagicMock()

try:
    from defaults.backend.stores.gog import GOGAPIClient
except ImportError as e:
    logger.error(f"Failed to import GOGAPIClient: {e}")
    sys.exit(1)

async def test_auth_conversion():
    logger.info("\n=== Testing Auth Conversion ===")
    
    # Create temp directory for config
    with tempfile.TemporaryDirectory() as temp_dir:
        token_file = os.path.join(temp_dir, "gog_token.json")
        cred_file = os.path.join(temp_dir, "gog_credentials.json")
        
        # Mock paths in GOGAPIClient
        client = GOGAPIClient(plugin_dir=os.getcwd())
        client.token_file = token_file
        client.gogdl_config_path = cred_file
        
        # Create dummy token file
        dummy_token = {
            "access_token": "test_access_token_123",
            "refresh_token": "test_refresh_token_456"
        }
        with open(token_file, 'w') as f:
            json.dump(dummy_token, f)
            
        # Load tokens
        client._load_tokens()
        
        # Run conversion
        success = client._ensure_auth_config()
        
        if success:
            logger.info("Auth conversion returned success")
            if os.path.exists(cred_file):
                with open(cred_file, 'r') as f:
                    data = json.load(f)
                    
                client_id = "46899977096215655"
                if client_id in data:
                    cred = data[client_id]
                    if cred['access_token'] == "test_access_token_123":
                        if 'loginTime' in cred:
                            logger.info("PASS: Credentials converted correctly (loginTime present)")
                        else:
                            logger.error("FAIL: loginTime missing from credentials")
                    else:
                        logger.error(f"FAIL: Access token mismatch: {cred['access_token']}")
                else:
                    logger.error("FAIL: JSON structure incorrect (missing client_id key)")
            else:
                logger.error("FAIL: Credentials file not created")
        else:
            logger.error("FAIL: _ensure_auth_config returned False")

async def test_exe_detection_logic():
    logger.info("\n=== Testing Executable Detection Logic ===")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        client = GOGAPIClient(plugin_dir=os.getcwd())
        
        # Test Case 1: Simple Windows Game (goggame-*.info)
        game_dir_1 = os.path.join(temp_dir, "Simple Game")
        os.makedirs(game_dir_1)
        
        info_content = {
            "playTasks": [
                {"isPrimary": True, "type": "FileTask", "path": "game.exe", "workingDir": ""}
            ]
        }
        with open(os.path.join(game_dir_1, "goggame-123.info"), 'w') as f:
            json.dump(info_content, f)
        
        # Create dummy exe
        with open(os.path.join(game_dir_1, "game.exe"), 'w') as f: f.write("exe")
        
        exe, work = client._find_game_executable_with_workdir(game_dir_1)
        if exe == os.path.join(game_dir_1, "game.exe"):
            logger.info("PASS: Detected Simple Windows EXE")
        else:
            logger.error(f"FAIL: Detected {exe}")

        # Test Case 2: Linux Game with start.sh
        game_dir_2 = os.path.join(temp_dir, "Linux Game")
        os.makedirs(game_dir_2)
        with open(os.path.join(game_dir_2, "start.sh"), 'w') as f: f.write("sh")
        
        exe, work = client._find_game_executable_with_workdir(game_dir_2)
        if exe == os.path.join(game_dir_2, "start.sh"):
            logger.info("PASS: Detected Linux start.sh")
        else:
            logger.error(f"FAIL: Detected {exe}")

        # Test Case 3: Tricky Path with Spaces & Brackets
        game_dir_3 = os.path.join(temp_dir, "Tricky Game [v1.0] (GOG)")
        os.makedirs(game_dir_3)
        
        info_content_3 = {
            "playTasks": [
                {"isPrimary": True, "type": "FileTask", "path": "bin/game_x64.exe", "workingDir": "bin"}
            ]
        }
        with open(os.path.join(game_dir_3, "goggame-456.info"), 'w') as f:
            json.dump(info_content_3, f)
            
        os.makedirs(os.path.join(game_dir_3, "bin"))
        with open(os.path.join(game_dir_3, "bin/game_x64.exe"), 'w') as f: f.write("exe")
        
        exe, work = client._find_game_executable_with_workdir(game_dir_3)
        expected = os.path.join(game_dir_3, "bin/game_x64.exe")
        if exe == expected:
            logger.info("PASS: Detected Tricky Path EXE")
        else:
            logger.error(f"FAIL: Expected {expected}, got {exe}")

async def test_live_gogdl():
    logger.info("\n=== Testing Live gogdl Interaction ===")
    
    # Check if gogdl is executable (it should be from previous steps)
    gogdl_path = os.path.abspath("bin/gogdl")
    if not os.access(gogdl_path, os.X_OK):
        # try fix
        os.chmod(gogdl_path, 0o755)
    
    client = GOGAPIClient(plugin_dir=os.getcwd())
    
    if not client.gogdl_bin or not os.path.exists(client.gogdl_bin):
        logger.error("FAIL: gogdl binary not found by client")
        return

    # Check if we have real tokens to test with
    real_token_exists = os.path.exists(os.path.expanduser("~/.config/unifideck/gog_token.json"))
    
    # 3. Test Live Info Fetch
    if real_token_exists:
        logger.info("Found real token file, attempting live 'info' fetch...")
        
        # Refresh token if needed
        client._load_tokens()
        if await client.is_available():
             logger.info("Token validated/refreshed successfully")
        else:
             logger.warning("Token validation failed - live tests might fail")
        
        # Ensure gogdl config is synced (is_available refreshes internal state, this writes it to gogdl config)
        client._ensure_auth_config()
        
        # Test getting size (which uses the same logic)
        game_id = "1423058311" # Bastion (Confirmed owned by dev account)
        logger.info(f"Fetching info for Game ID {game_id} (Bastion)...")
        size = await client.get_game_size(game_id)
        
        if size:
            logger.info(f"PASS: Fetched size: {size} bytes")
        else:
            logger.warning("WARN: Failed to fetch size (Auth might be invalid or network issue)")
            
        # Also manually verify info command output for folder_name
        cmd = [
            client.gogdl_bin, '--auth-config-path', client.gogdl_config_path,
            'info', '--platform', 'linux', game_id
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            found_folder = False
            for line in stdout.decode().split('\n'):
                if 'folder_name' in line:
                    logger.info(f"PASS: gogdl info contains folder_name: {line.strip()}")
                    found_folder = True
                    break
            if not found_folder:
                logger.warning("WARN: gogdl info output did not contain folder_name (might be expected for this version?)")
        
    else:
        logger.info("SKIP: No real token file found, skipping live gogdl test")

async def test_nested_directory_logic():
    logger.info("\n=== Testing Nested Directory Logic ===")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        client = GOGAPIClient(plugin_dir=os.getcwd())
        
        # Test Case: Game with nested 'game' folder (like Bastion)
        game_base = os.path.join(temp_dir, "Bastion")
        game_nested = os.path.join(game_base, "game")
        os.makedirs(game_nested)
        
        info_content = {
            "playTasks": [
                {"isPrimary": True, "type": "FileTask", "path": "Bastion", "workingDir": ""}
            ]
        }
        # Info file inside nested game folder
        with open(os.path.join(game_nested, "goggame-1423058311.info"), 'w') as f:
            json.dump(info_content, f)
            
        # Executable inside nested game folder
        exe_path = os.path.join(game_nested, "Bastion")
        with open(exe_path, 'w') as f: f.write("elf")
        os.chmod(exe_path, 0o755)
        
        # 1. Test _get_game_id_from_dir
        # Should find ID from base folder by looking into nested
        found_id = client._get_game_id_from_dir(game_base)
        if found_id == "1423058311":
             logger.info("PASS: _get_game_id_from_dir found nested info file")
        else:
             logger.error(f"FAIL: _get_game_id_from_dir returned {found_id}")
             
        # 2. Test _find_game_executable_with_workdir
        # Should find executable path
        exe_found, work_found = client._find_game_executable_with_workdir(game_base)
        if exe_found == exe_path:
            logger.info("PASS: _find_game_executable_with_workdir found nested executable")
        else:
            logger.error(f"FAIL: _find_game_executable_with_workdir returned {exe_found}, expected {exe_path}")


async def main():
    await test_auth_conversion()
    await test_exe_detection_logic()
    await test_nested_directory_logic()
    await test_live_gogdl()

if __name__ == "__main__":
    asyncio.run(main())
