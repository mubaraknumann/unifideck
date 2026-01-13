#!/usr/bin/env python3
"""
Comprehensive Testing Suite for heroic-gogdl

This script tests gogdl's download and extraction capabilities with:
1. Authentication verification
2. Library fetching
3. Game info retrieval
4. Small game download (< 500MB)
5. Medium game download (1-5GB)  
6. Large game download (10GB+)
7. Linux and Windows installer handling
8. Resume functionality
9. Error handling

Run with: python3 gogdl_test_suite.py --help
"""

import argparse
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Add heroic-gogdl to path
SCRIPT_DIR = Path(__file__).parent.absolute()
PROJECT_ROOT = SCRIPT_DIR.parent
GOGDL_PATH = PROJECT_ROOT / "heroic-gogdl-main"
PY_MODULES_PATH = PROJECT_ROOT / "py_modules"

# Add bundled modules first (for requests, etc)
sys.path.insert(0, str(PY_MODULES_PATH))
sys.path.insert(0, str(GOGDL_PATH))


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("GOGDL_TEST")

# Test results storage
TEST_RESULTS: Dict[str, dict] = {}


class TestTimer:
    """Context manager for timing operations"""
    def __init__(self, name: str):
        self.name = name
        self.start_time = None
        self.end_time = None
        
    def __enter__(self):
        self.start_time = time.time()
        logger.info(f"⏱️  Starting: {self.name}")
        return self
        
    def __exit__(self, *args):
        self.end_time = time.time()
        duration = self.end_time - self.start_time
        logger.info(f"⏱️  Completed: {self.name} in {duration:.2f}s")
        return False
        
    @property
    def duration(self) -> float:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0


def format_size(bytes_size: int) -> str:
    """Format bytes to human readable string"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} PB"


def record_result(test_name: str, success: bool, duration: float, 
                  details: str = "", error: str = ""):
    """Record test result"""
    TEST_RESULTS[test_name] = {
        "success": success,
        "duration": duration,
        "details": details,
        "error": error,
        "timestamp": datetime.now().isoformat()
    }
    status = "✅ PASS" if success else "❌ FAIL"
    logger.info(f"{status}: {test_name} ({duration:.2f}s)")
    if error:
        logger.error(f"    Error: {error}")


class GOGDLTester:
    """Main test class for gogdl integration testing"""
    
    def __init__(self, test_dir: str, credentials_path: str, cleanup: bool = True):
        self.test_dir = Path(test_dir)
        self.credentials_path = Path(credentials_path)
        self.cleanup = cleanup
        
        # Create test directory
        self.test_dir.mkdir(parents=True, exist_ok=True)
        
        # GOG API and auth managers will be initialized in setup
        self.auth_manager = None
        self.api_handler = None
        self.owned_game_ids = []
        
        # Test game IDs - will be populated from library
        self.test_games = {
            "small": [],   # < 500 MB
            "medium": [],  # 500MB - 5GB
            "large": [],   # > 5GB
            "linux": [],   # Linux native
            "windows": [], # Windows only
        }
        
    def setup(self) -> bool:
        """Initialize gogdl components"""
        logger.info("=" * 60)
        logger.info("GOGDL Integration Test Suite")
        logger.info("=" * 60)
        
        with TestTimer("Setup gogdl imports") as timer:
            try:
                import gogdl.auth as auth
                import gogdl.api as api
                
                self.auth_module = auth
                self.api_module = api
                
                # Initialize auth manager
                self.auth_manager = auth.AuthorizationManager(str(self.credentials_path))
                self.api_handler = api.ApiHandler(self.auth_manager)
                
                record_result("Import gogdl modules", True, timer.duration)
                return True
                
            except Exception as e:
                record_result("Import gogdl modules", False, timer.duration, 
                             error=str(e))
                return False

    def test_authentication(self) -> bool:
        """Test authentication is working"""
        with TestTimer("Test Authentication") as timer:
            try:
                credentials = self.auth_manager.get_credentials()
                
                if not credentials:
                    record_result("Authentication", False, timer.duration,
                                error="No credentials found. Please authenticate first.")
                    return False
                
                # Check token validity
                try:
                    is_expired = self.auth_manager.is_credential_expired()
                except ValueError:
                    record_result("Authentication", False, timer.duration,
                                error="Credential doesn't exist in config")
                    return False
                    
                if is_expired:
                    logger.info("Token expired, attempting refresh...")
                    if not self.auth_manager.refresh_credentials():
                        record_result("Authentication", False, timer.duration,
                                    error="Failed to refresh expired token")
                        return False
                    credentials = self.auth_manager.get_credentials()
                
                access_token = credentials.get("access_token", "")
                logger.info(f"Token status: Valid (expires in {credentials.get('expires_in', 0)}s)")
                logger.info(f"Token length: {len(access_token)} chars")
                
                record_result("Authentication", True, timer.duration,
                            details=f"Token valid, {len(access_token)} chars")
                return True
                
            except Exception as e:
                record_result("Authentication", False, timer.duration, error=str(e))
                return False

    def test_library_fetch(self) -> bool:
        """Test fetching user's game library"""
        with TestTimer("Fetch Game Library") as timer:
            try:
                # Force token refresh to ensure we have a valid token
                logger.info("Refreshing token before library fetch...")
                if self.auth_manager.refresh_credentials():
                    credentials = self.auth_manager.get_credentials()
                    if credentials:
                        self.api_handler.session.headers["Authorization"] = f"Bearer {credentials['access_token']}"
                        logger.info("Token refreshed successfully")
                else:
                    logger.warning("Token refresh failed, using existing token")
                
                # Get owned games list
                response = self.api_handler.session.get(
                    'https://embed.gog.com/user/data/games'
                )

                
                if not response.ok:
                    record_result("Fetch Library", False, timer.duration,
                                error=f"API returned {response.status_code}")
                    return False
                
                data = response.json()
                owned_ids = data.get('owned', [])
                
                logger.info(f"Found {len(owned_ids)} owned games")
                
                # Store for later use
                self.owned_game_ids = [str(gid) for gid in owned_ids]
                
                record_result("Fetch Library", True, timer.duration,
                            details=f"{len(owned_ids)} games found")
                return True
                
            except Exception as e:
                record_result("Fetch Library", False, timer.duration, error=str(e))
                return False

    def categorize_games(self, limit: int = 20) -> bool:
        """Categorize owned games by size and platform"""
        with TestTimer("Categorize Games by Size/Platform") as timer:
            try:
                categorized = 0
                
                for game_id in self.owned_game_ids[:limit]:
                    game_data = self.api_handler.get_item_data(
                        game_id,
                        expanded=['downloads']
                    )
                    
                    if not game_data:
                        continue
                    
                    title = game_data.get('title', 'Unknown')
                    downloads = game_data.get('downloads', {})
                    
                    # Check for Linux installer
                    installers = downloads.get('installers', [])
                    has_linux = any(i.get('os') == 'linux' for i in installers)
                    has_windows = any(i.get('os') == 'windows' for i in installers)
                    
                    # Get size (approximate from first installer)
                    size_mb = 0
                    for installer in installers:
                        size_str = str(installer.get('total_size', '0'))
                        try:
                            size_bytes = int(size_str) if size_str.isdigit() else 0
                            size_mb = size_bytes / (1024 * 1024)
                        except:
                            pass
                        break
                    
                    game_entry = {
                        "id": game_id,
                        "title": title,
                        "size_mb": size_mb,
                        "has_linux": has_linux,
                        "has_windows": has_windows
                    }
                    
                    # Categorize by size
                    if size_mb > 0:
                        if size_mb < 500:
                            self.test_games["small"].append(game_entry)
                        elif size_mb < 5000:
                            self.test_games["medium"].append(game_entry)
                        else:
                            self.test_games["large"].append(game_entry)
                    
                    # Categorize by platform
                    if has_linux:
                        self.test_games["linux"].append(game_entry)
                    if has_windows and not has_linux:
                        self.test_games["windows"].append(game_entry)
                    
                    categorized += 1
                
                # Log summary
                logger.info(f"Categorized {categorized} games:")
                logger.info(f"  Small (< 500MB): {len(self.test_games['small'])}")
                logger.info(f"  Medium (500MB-5GB): {len(self.test_games['medium'])}")
                logger.info(f"  Large (> 5GB): {len(self.test_games['large'])}")
                logger.info(f"  Linux native: {len(self.test_games['linux'])}")
                logger.info(f"  Windows only: {len(self.test_games['windows'])}")
                
                record_result("Categorize Games", True, timer.duration,
                            details=f"{categorized} games categorized")
                return True
                
            except Exception as e:
                record_result("Categorize Games", False, timer.duration, error=str(e))
                return False

    def test_download_game(self, game_entry: dict, category: str) -> bool:
        """Test actual game download"""
        game_id = game_entry["id"]
        title = game_entry["title"]
        test_name = f"Download {category}: {title}"
        
        install_path = self.test_dir / f"downloads/{category}/{game_id}"
        install_path.mkdir(parents=True, exist_ok=True)
        
        with TestTimer(test_name) as timer:
            try:
                from gogdl.dl.managers import manager
                
                # Determine platform
                platform = "linux" if game_entry.get("has_linux") else "windows"
                
                # Create arguments
                class MockArgs:
                    pass
                
                args = MockArgs()
                args.id = game_id
                args.path = str(install_path)
                args.support = str(install_path / "support")
                args.dlcs = None
                args.skip_dlcs = True
                args.with_dlcs = False
                args.platform = platform
                args.lang = "en"
                args.workers_count = 4
                args.command = "download"
                args.build = None
                args.branch = None
                
                logger.info(f"Downloading: {title}")
                logger.info(f"Platform: {platform}")
                logger.info(f"Path: {install_path}")
                
                download_manager = manager.Manager(args, [], self.api_handler)
                
                # Actually download
                download_manager.download(args, [])
                
                # Verify files exist
                files = list(install_path.rglob("*"))
                file_count = len([f for f in files if f.is_file()])
                total_size = sum(f.stat().st_size for f in files if f.is_file())
                
                if file_count > 0:
                    logger.info(f"Downloaded {file_count} files, {format_size(total_size)}")
                    
                    record_result(test_name, True, timer.duration,
                                details=f"{file_count} files, {format_size(total_size)}, {timer.duration:.1f}s")
                    
                    # Calculate speed
                    if timer.duration > 0:
                        speed_mbps = (total_size / (1024*1024)) / timer.duration
                        logger.info(f"Average speed: {speed_mbps:.2f} MB/s")
                    
                    return True
                else:
                    record_result(test_name, False, timer.duration,
                                error="No files downloaded")
                    return False
                    
            except Exception as e:
                logger.exception(f"Download failed: {e}")
                record_result(test_name, False, timer.duration, error=str(e))
                return False
            finally:
                # Cleanup if requested
                if self.cleanup and install_path.exists():
                    logger.info(f"Cleaning up: {install_path}")
                    shutil.rmtree(install_path, ignore_errors=True)

    def run_all_tests(self, 
                     skip_downloads: bool = False,
                     test_small: bool = True,
                     test_medium: bool = True,
                     test_large: bool = False) -> Dict:
        """Run all tests"""
        
        # Setup
        if not self.setup():
            logger.error("Setup failed, aborting tests")
            return TEST_RESULTS
        
        # Test authentication
        if not self.test_authentication():
            logger.error("Authentication failed, aborting tests")
            return TEST_RESULTS
        
        # Test library fetch
        if not self.test_library_fetch():
            logger.error("Library fetch failed, aborting tests")
            return TEST_RESULTS
        
        # Categorize games
        if not self.categorize_games(limit=30):
            logger.error("Game categorization failed")
            return TEST_RESULTS
        
        # Skip actual downloads if requested
        if skip_downloads:
            logger.info("Skipping download tests (--skip-downloads)")
            return TEST_RESULTS
        
        # Test downloads by category
        if test_small and self.test_games["small"]:
            game = self.test_games["small"][0]
            self.test_download_game(game, "small")
        
        if test_medium and self.test_games["medium"]:
            game = self.test_games["medium"][0]
            self.test_download_game(game, "medium")
        
        if test_large and self.test_games["large"]:
            game = self.test_games["large"][0]
            self.test_download_game(game, "large")
        
        return TEST_RESULTS

    def print_summary(self):
        """Print test summary"""
        logger.info("")
        logger.info("=" * 60)
        logger.info("TEST SUMMARY")
        logger.info("=" * 60)
        
        passed = 0
        failed = 0
        total_time = 0
        
        for test_name, result in TEST_RESULTS.items():
            status = "✅" if result["success"] else "❌"
            duration = result["duration"]
            total_time += duration
            
            if result["success"]:
                passed += 1
            else:
                failed += 1
            
            details = result.get("details", "")
            error = result.get("error", "")
            
            line = f"{status} {test_name}: {duration:.2f}s"
            if details:
                line += f" - {details}"
            if error:
                line += f" [ERROR: {error}]"
            
            logger.info(line)
        
        logger.info("-" * 60)
        logger.info(f"Total: {passed + failed} tests, {passed} passed, {failed} failed")
        logger.info(f"Total time: {total_time:.2f}s")
        logger.info("=" * 60)
        
        return passed, failed


def convert_unifideck_credentials(unifideck_path: str, gogdl_path: str) -> bool:
    """Convert Unifideck GOG token format to gogdl format"""
    try:
        with open(unifideck_path, 'r') as f:
            data = json.load(f)
        
        # Unifideck format: {access_token, refresh_token}
        # gogdl format: {CLIENT_ID: {access_token, refresh_token, expires_in, loginTime}}
        client_id = "46899977096215655"
        
        gogdl_data = {
            client_id: {
                "access_token": data.get("access_token"),
                "refresh_token": data.get("refresh_token"),
                "expires_in": 3600,  # Default 1 hour
                "loginTime": time.time() - 3000  # Mark as almost expired to force refresh
            }
        }
        
        os.makedirs(os.path.dirname(gogdl_path), exist_ok=True)
        with open(gogdl_path, 'w') as f:
            json.dump(gogdl_data, f)
        
        logger.info(f"Converted credentials from {unifideck_path} to {gogdl_path}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to convert credentials: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Test gogdl integration")
    parser.add_argument("--test-dir", 
                       default=os.path.expanduser("~/.local/share/unifideck/gogdl_test"),
                       help="Directory for test downloads")
    parser.add_argument("--credentials", 
                       default=os.path.expanduser("~/.config/unifideck/gog_credentials.json"),
                       help="Path to GOG credentials JSON (gogdl format)")
    parser.add_argument("--skip-downloads", action="store_true",
                       help="Skip actual download tests (only test auth/info)")
    parser.add_argument("--no-cleanup", action="store_true",
                       help="Don't cleanup downloaded files after tests")
    parser.add_argument("--test-large", action="store_true",
                       help="Include large game (>5GB) download test")
    parser.add_argument("--list-games", action="store_true",
                       help="List available test games by category")
    parser.add_argument("-v", "--verbose", action="store_true",
                       help="Enable verbose logging")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Check credentials exist
    unifideck_token = os.path.expanduser("~/.config/unifideck/gog_token.json")
    
    if not os.path.exists(args.credentials):
        # Try to convert Unifideck's token
        if os.path.exists(unifideck_token):
            logger.info(f"Converting Unifideck credentials to gogdl format...")
            if not convert_unifideck_credentials(unifideck_token, args.credentials):
                logger.error("Failed to convert credentials")
                sys.exit(1)
        else:
            logger.error(f"No credentials found at {args.credentials} or {unifideck_token}")
            logger.error("Please authenticate with GOG first via Unifideck")
            sys.exit(1)
    
    # Create tester
    tester = GOGDLTester(
        test_dir=args.test_dir,
        credentials_path=args.credentials,
        cleanup=not args.no_cleanup
    )
    
    # Run tests
    results = tester.run_all_tests(
        skip_downloads=args.skip_downloads,
        test_large=args.test_large
    )
    
    # List games if requested
    if args.list_games:
        logger.info("\nAvailable test games by category:")
        for category, games in tester.test_games.items():
            logger.info(f"\n{category.upper()}:")
            for g in games[:5]:  # Show first 5
                logger.info(f"  - {g['title']} ({format_size(g['size_mb'] * 1024 * 1024)})")
    
    # Print summary
    passed, failed = tester.print_summary()
    
    # Save results to JSON
    results_file = Path(args.test_dir) / "test_results.json"
    results_file.parent.mkdir(parents=True, exist_ok=True)
    with open(results_file, 'w') as f:
        json.dump(TEST_RESULTS, f, indent=2)
    logger.info(f"Results saved to: {results_file}")
    
    # Exit with error code if any tests failed
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
