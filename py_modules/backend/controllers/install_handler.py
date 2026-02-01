"""Game installation handler across stores.

Handles Epic/GOG/Amazon game installations and uninstallations.
"""

import os
import json
import asyncio
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class InstallHandler:
    """Handles game installations across stores"""

    def __init__(self, shortcuts_manager: ShortcutsManager, plugin_dir: Optional[str] = None):
        self.shortcuts_manager = shortcuts_manager
        self.plugin_dir = plugin_dir

    async def get_epic_game_exe(self, game_id: str) -> Optional[str]:
        """Get executable path for installed Epic game"""
        legendary_bin = EpicConnector(plugin_dir=self.plugin_dir)._find_legendary()
        if not legendary_bin:
            return None

        try:
            # Get game info in JSON format
            proc = await asyncio.create_subprocess_exec(
                legendary_bin, 'info', game_id, '--json',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                info = json.loads(stdout.decode())
                install_path = info.get('install', {}).get('install_path', '')
                executable = info.get('manifest', {}).get('launch_exe', '')

                if install_path and executable:
                    # Strip leading slash - legendary returns paths like '/Binaries/Win64/Game.exe'
                    # which causes os.path.join to treat it as absolute, ignoring install_path
                    executable = executable.lstrip('/')
                    exe_path = os.path.join(install_path, executable)
                    logger.info(f"Found Epic game executable: {exe_path}")
                    return exe_path

        except Exception as e:
            logger.error(f"Error getting Epic game exe: {e}")

        return None

    async def install_epic_game(self, game_id: str, install_path: Optional[str] = None) -> Dict[str, Any]:
        """Install Epic game via legendary"""
        legendary_bin = EpicConnector(plugin_dir=self.plugin_dir)._find_legendary()
        if not legendary_bin:
            return {'success': False, 'error': 'legendary not found'}

        try:
            cmd = [legendary_bin, 'install', game_id, '--yes']
            if install_path:
                cmd.extend(['--base-path', install_path])

            logger.info(f"Installing Epic game: {' '.join(cmd)}")

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                # Get actual executable path
                exe_path = await self.get_epic_game_exe(game_id)

                if exe_path:
                    # Extract install directory from exe path
                    import os.path
                    install_dir = os.path.dirname(exe_path)

                    # Update shortcuts.vdf with install info
                    await self.shortcuts_manager.mark_installed(game_id, 'epic', install_dir, exe_path)
                else:
                    # Fallback: keep launcher script
                    logger.warning(f"Could not find exe for {game_id}, keeping launcher script")

                logger.info(f"Successfully installed {game_id}")
                return {'success': True, 'exe_path': exe_path}
            else:
                logger.error(f"Install failed: {stderr.decode()}")
                return {'success': False, 'error': stderr.decode()}

        except Exception as e:
            logger.error(f"Error installing Epic game: {e}")
            return {'success': False, 'error': str(e)}

    async def get_gog_game_exe(self, game_id: str, install_dir: str) -> Optional[str]:
        """Find executable for GOG game"""
        # Look for start.sh or other launch scripts
        common_launchers = ['start.sh', 'launch.sh', f'{game_id}.sh']

        for launcher in common_launchers:
            launcher_path = os.path.join(install_dir, launcher)
            if os.path.exists(launcher_path):
                logger.info(f"Found GOG game launcher: {launcher_path}")
                return launcher_path

        # Try to find any .sh file in the directory
        try:
            for item in os.listdir(install_dir):
                if item.endswith('.sh') and os.path.isfile(os.path.join(install_dir, item)):
                    launcher_path = os.path.join(install_dir, item)
                    logger.info(f"Found GOG game script: {launcher_path}")
                    return launcher_path
        except Exception as e:
            logger.error(f"Error searching for GOG launcher: {e}")

        return None

    async def install_gog_game(self, game_id: str, gog_instance, install_path: Optional[str] = None) -> Dict[str, Any]:
        """Install GOG game using GOG API

        Args:
            game_id: GOG game product ID
            gog_instance: Instance of GOG class with API methods
            install_path: Optional custom install path (not used - GOG class manages this)

        Returns:
            Dict with success status and exe_path
        """
        try:
            # Use the GOG class's install_game method which uses the API
            result = await gog_instance.install_game(game_id)

            if result.get('success'):
                # Update shortcuts.vdf with the installed game info
                exe_path = result.get('executable')
                install_dir = result.get('install_path')
                work_dir = result.get('work_dir')  # From goggame-*.info

                if install_dir:
                    await self.shortcuts_manager.mark_installed(game_id, 'gog', install_dir, exe_path, work_dir)
                    logger.info(f"Successfully installed GOG game {game_id} with work_dir={work_dir}")
                    return {'success': True, 'exe_path': exe_path, 'install_path': install_dir, 'work_dir': work_dir}

            return result

        except Exception as e:
            logger.error(f"Error installing GOG game: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    async def get_amazon_game_exe(self, game_id: str, install_dir: str = None) -> Optional[str]:
        """Find executable for Amazon game using fuel.json"""
        # If no install_dir provided, try to find from nile config
        if not install_dir:
            nile_config = os.path.expanduser("~/.config/nile")
            installed_file = os.path.join(nile_config, "installed.json")
            
            if os.path.exists(installed_file):
                try:
                    with open(installed_file, 'r') as f:
                        installed_list = json.load(f)
                    
                    for game in installed_list:
                        if game.get('id') == game_id:
                            install_dir = game.get('path', '')
                            break
                except Exception as e:
                    logger.error(f"[Amazon] Error reading installed.json: {e}")
        
        if not install_dir:
            logger.warning(f"[Amazon] Could not find install directory for {game_id}")
            return None
        
        # Parse fuel.json for executable
        fuel_path = os.path.join(install_dir, 'fuel.json')
        if not os.path.exists(fuel_path):
            logger.warning(f"[Amazon] No fuel.json found at {fuel_path}")
            return None
        
        try:
            import re
            with open(fuel_path, 'r') as f:
                content = f.read()
                # Remove single-line comments (fuel.json may have them)
                content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
                fuel_data = json.loads(content)
            
            main_cmd = fuel_data.get('Main', {}).get('Command', '')
            if main_cmd:
                exe_path = os.path.join(install_dir, main_cmd)
                logger.info(f"[Amazon] Found executable from fuel.json: {exe_path}")
                return exe_path
        except Exception as e:
            logger.error(f"[Amazon] Error parsing fuel.json: {e}")
        
        return None

    async def install_amazon_game(self, game_id: str, amazon_instance, install_path: Optional[str] = None) -> Dict[str, Any]:
        """Install Amazon game using nile CLI

        Args:
            game_id: Amazon game product ID
            amazon_instance: Instance of AmazonConnector with install methods
            install_path: Optional custom install path

        Returns:
            Dict with success status and exe_path
        """
        try:
            # Use the AmazonConnector's install_game method
            result = await amazon_instance.install_game(game_id)

            if result.get('success'):
                # Update shortcuts.vdf with the installed game info
                exe_path = result.get('exe_path')
                install_dir = result.get('install_path')

                if install_dir:
                    await self.shortcuts_manager.mark_installed(game_id, 'amazon', install_dir, exe_path)
                    logger.info(f"Successfully installed Amazon game {game_id}")
                    return {'success': True, 'exe_path': exe_path, 'install_path': install_dir}

            return result

        except Exception as e:
            logger.error(f"Error installing Amazon game: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}
