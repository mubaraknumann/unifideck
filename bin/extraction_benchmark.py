#!/usr/bin/env python3
"""
Comprehensive Extraction Tools Testing Suite

Tests various extraction tools available on Steam Deck:
1. unzip - Standard Unix tool
2. 7z/7za - 7-Zip (if available)
3. bsdtar - BSD tar with libarchive
4. busybox unzip - BusyBox implementation
5. Python zipfile - Current implementation
6. gogdl binary - If available

Also tests heroic-gogdl as CLI binary.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("EXTRACT_TEST")

# Test results
RESULTS: Dict[str, dict] = {}


def format_size(bytes_size: int) -> str:
    """Format bytes to human readable string"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} TB"


def format_time(seconds: float) -> str:
    """Format seconds to human readable string"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        return f"{seconds/60:.1f}m"
    else:
        return f"{seconds/3600:.1f}h"


class ExtractionTester:
    """Test various extraction tools"""
    
    def __init__(self, test_dir: str):
        self.test_dir = Path(test_dir)
        self.test_dir.mkdir(parents=True, exist_ok=True)
        
        # Track tool availability
        self.tools: Dict[str, dict] = {}
        
    def check_tool_availability(self):
        """Check which extraction tools are available"""
        logger.info("=" * 60)
        logger.info("CHECKING TOOL AVAILABILITY")
        logger.info("=" * 60)
        
        tools_to_check = [
            ("unzip", ["unzip", "-v"], "Standard unzip"),
            ("7z", ["7z", "--help"], "7-Zip"),
            ("7za", ["7za", "--help"], "7-Zip standalone"),
            ("7zr", ["7zr", "--help"], "7-Zip reduced"),
            ("bsdtar", ["bsdtar", "--version"], "BSD tar (libarchive)"),
            ("tar", ["tar", "--version"], "GNU tar"),
            ("busybox", ["busybox", "--list"], "BusyBox"),
            ("pigz", ["pigz", "--version"], "Parallel gzip"),
            ("unpigz", ["unpigz", "--version"], "Parallel gunzip"),
            ("lz4", ["lz4", "--version"], "LZ4 compression"),
            ("zstd", ["zstd", "--version"], "Zstandard compression"),
            ("innoextract", ["innoextract", "--version"], "InnoSetup extractor"),
            ("gogdl", ["gogdl", "--version"], "GOG Downloader"),
        ]
        
        for tool_name, check_cmd, description in tools_to_check:
            try:
                result = subprocess.run(
                    check_cmd,
                    capture_output=True,
                    timeout=5
                )
                available = result.returncode in [0, 1]  # Some tools return 1 for --help
                
                # Get version from output
                version_output = result.stdout.decode() or result.stderr.decode()
                version = version_output.split('\n')[0][:80] if version_output else "unknown"
                
                # Find actual path
                which_result = subprocess.run(["which", tool_name], capture_output=True)
                path = which_result.stdout.decode().strip() if which_result.returncode == 0 else "not found"
                
                self.tools[tool_name] = {
                    "available": available,
                    "path": path,
                    "version": version,
                    "description": description
                }
                
                status = "✅" if available else "❌"
                logger.info(f"{status} {tool_name}: {path}")
                
            except FileNotFoundError:
                self.tools[tool_name] = {
                    "available": False,
                    "path": "not found",
                    "version": "",
                    "description": description
                }
                logger.info(f"❌ {tool_name}: not found")
            except subprocess.TimeoutExpired:
                self.tools[tool_name] = {
                    "available": False,
                    "path": "timeout",
                    "version": "",
                    "description": description
                }
                logger.info(f"⚠️ {tool_name}: timeout")
        
        # Summary
        available_count = sum(1 for t in self.tools.values() if t["available"])
        logger.info(f"\nTotal: {available_count}/{len(self.tools)} tools available")
        
        return self.tools

    def create_test_archive(self, size_mb: int = 100) -> Optional[Path]:
        """Create a test ZIP archive with random data"""
        logger.info(f"\nCreating {size_mb}MB test archive...")
        
        try:
            import zipfile
            import random
            
            # Create temp directory with test files
            source_dir = self.test_dir / "test_source"
            source_dir.mkdir(exist_ok=True)
            
            # Create files with ~10MB each
            files_needed = max(1, size_mb // 10)
            bytes_per_file = (size_mb * 1024 * 1024) // files_needed
            
            for i in range(files_needed):
                file_path = source_dir / f"testfile_{i:03d}.bin"
                with open(file_path, 'wb') as f:
                    # Write random-ish data (compressible)
                    chunk = bytes([random.randint(0, 255) for _ in range(4096)])
                    for _ in range(bytes_per_file // 4096):
                        f.write(chunk)
            
            # Create ZIP archive
            archive_path = self.test_dir / "test_archive.zip"
            start = time.time()
            
            with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for file_path in source_dir.rglob("*"):
                    if file_path.is_file():
                        zf.write(file_path, file_path.relative_to(source_dir))
            
            duration = time.time() - start
            size = archive_path.stat().st_size
            
            logger.info(f"Created: {archive_path} ({format_size(size)}) in {duration:.1f}s")
            
            # Cleanup source
            shutil.rmtree(source_dir)
            
            return archive_path
            
        except Exception as e:
            logger.error(f"Failed to create test archive: {e}")
            return None

    async def test_extraction_tool(self, tool_name: str, archive_path: Path, 
                                   extract_cmd: List[str]) -> dict:
        """Test a single extraction tool"""
        extract_dir = self.test_dir / f"extract_{tool_name}"
        
        # Cleanup previous
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir()
        
        result = {
            "tool": tool_name,
            "success": False,
            "duration": 0,
            "files_extracted": 0,
            "bytes_extracted": 0,
            "error": "",
            "speed_mbps": 0
        }
        
        try:
            # Build command with actual paths
            cmd = [
                c.replace("{archive}", str(archive_path))
                 .replace("{output}", str(extract_dir))
                for c in extract_cmd
            ]
            
            logger.info(f"Testing {tool_name}: {' '.join(cmd[:5])}...")
            
            start = time.time()
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(extract_dir)
            )
            
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            
            duration = time.time() - start
            
            if proc.returncode == 0:
                # Count extracted files
                files = list(extract_dir.rglob("*"))
                file_count = len([f for f in files if f.is_file()])
                total_bytes = sum(f.stat().st_size for f in files if f.is_file())
                
                result["success"] = True
                result["duration"] = duration
                result["files_extracted"] = file_count
                result["bytes_extracted"] = total_bytes
                result["speed_mbps"] = (total_bytes / (1024*1024)) / duration if duration > 0 else 0
                
                logger.info(f"  ✅ {format_time(duration)} - {file_count} files, {format_size(total_bytes)}, {result['speed_mbps']:.1f} MB/s")
            else:
                result["error"] = stderr.decode()[:200]
                logger.info(f"  ❌ Failed: {result['error'][:100]}")
                
        except asyncio.TimeoutError:
            result["error"] = "Timeout (300s)"
            logger.info(f"  ⚠️ Timeout after 300s")
        except Exception as e:
            result["error"] = str(e)
            logger.info(f"  ❌ Error: {e}")
        finally:
            # Cleanup
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
        
        return result

    async def test_python_zipfile(self, archive_path: Path) -> dict:
        """Test Python's zipfile module (current implementation)"""
        extract_dir = self.test_dir / "extract_python_zipfile"
        
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir()
        
        result = {
            "tool": "python_zipfile",
            "success": False,
            "duration": 0,
            "files_extracted": 0,
            "bytes_extracted": 0,
            "error": "",
            "speed_mbps": 0
        }
        
        try:
            import zipfile
            
            logger.info("Testing python_zipfile...")
            
            start = time.time()
            
            with zipfile.ZipFile(archive_path, 'r') as zf:
                zf.extractall(extract_dir)
            
            duration = time.time() - start
            
            # Count extracted files
            files = list(extract_dir.rglob("*"))
            file_count = len([f for f in files if f.is_file()])
            total_bytes = sum(f.stat().st_size for f in files if f.is_file())
            
            result["success"] = True
            result["duration"] = duration
            result["files_extracted"] = file_count
            result["bytes_extracted"] = total_bytes
            result["speed_mbps"] = (total_bytes / (1024*1024)) / duration if duration > 0 else 0
            
            logger.info(f"  ✅ {format_time(duration)} - {file_count} files, {format_size(total_bytes)}, {result['speed_mbps']:.1f} MB/s")
            
        except Exception as e:
            result["error"] = str(e)
            logger.info(f"  ❌ Error: {e}")
        finally:
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
        
        return result

    async def test_python_zipfile_threaded(self, archive_path: Path) -> dict:
        """Test Python's zipfile with threading (proposed improvement)"""
        extract_dir = self.test_dir / "extract_python_threaded"
        
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir()
        
        result = {
            "tool": "python_zipfile_threaded",
            "success": False,
            "duration": 0,
            "files_extracted": 0,
            "bytes_extracted": 0,
            "error": "",
            "speed_mbps": 0
        }
        
        try:
            import zipfile
            from concurrent.futures import ThreadPoolExecutor
            
            logger.info("Testing python_zipfile (threaded)...")
            
            start = time.time()
            
            def extract_member(zf_path, member, extract_to):
                with zipfile.ZipFile(zf_path, 'r') as zf:
                    zf.extract(member, extract_to)
            
            with zipfile.ZipFile(archive_path, 'r') as zf:
                members = zf.namelist()
            
            # Use thread pool
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(extract_member, archive_path, m, extract_dir)
                    for m in members
                ]
                for f in futures:
                    f.result()
            
            duration = time.time() - start
            
            # Count extracted files
            files = list(extract_dir.rglob("*"))
            file_count = len([f for f in files if f.is_file()])
            total_bytes = sum(f.stat().st_size for f in files if f.is_file())
            
            result["success"] = True
            result["duration"] = duration
            result["files_extracted"] = file_count
            result["bytes_extracted"] = total_bytes
            result["speed_mbps"] = (total_bytes / (1024*1024)) / duration if duration > 0 else 0
            
            logger.info(f"  ✅ {format_time(duration)} - {file_count} files, {format_size(total_bytes)}, {result['speed_mbps']:.1f} MB/s")
            
        except Exception as e:
            result["error"] = str(e)
            logger.info(f"  ❌ Error: {e}")
        finally:
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
        
        return result

    async def run_all_tests(self, archive_size_mb: int = 100):
        """Run all extraction tests"""
        logger.info("=" * 60)
        logger.info("EXTRACTION TOOL BENCHMARK")
        logger.info("=" * 60)
        
        # Check tools
        self.check_tool_availability()
        
        # Create test archive
        archive = self.create_test_archive(archive_size_mb)
        if not archive:
            logger.error("Failed to create test archive, aborting")
            return {}
        
        archive_size = archive.stat().st_size
        logger.info(f"\nTest archive: {format_size(archive_size)}")
        logger.info("=" * 60)
        
        results = []
        
        # Test Python zipfile (baseline - current implementation)
        results.append(await self.test_python_zipfile(archive))
        
        # Test Python zipfile with threading
        results.append(await self.test_python_zipfile_threaded(archive))
        
        # Test unzip if available
        if self.tools.get("unzip", {}).get("available"):
            results.append(await self.test_extraction_tool(
                "unzip", archive,
                ["unzip", "-o", "-q", "{archive}", "-d", "{output}"]
            ))
        
        # Test 7z if available
        if self.tools.get("7z", {}).get("available"):
            results.append(await self.test_extraction_tool(
                "7z", archive,
                ["7z", "x", "-y", "-o{output}", "{archive}"]
            ))
        
        # Test 7za if available
        if self.tools.get("7za", {}).get("available"):
            results.append(await self.test_extraction_tool(
                "7za", archive,
                ["7za", "x", "-y", "-o{output}", "{archive}"]
            ))
        
        # Test bsdtar if available  
        if self.tools.get("bsdtar", {}).get("available"):
            results.append(await self.test_extraction_tool(
                "bsdtar", archive,
                ["bsdtar", "-xf", "{archive}", "-C", "{output}"]
            ))
        
        # Test busybox unzip if available
        if self.tools.get("busybox", {}).get("available"):
            results.append(await self.test_extraction_tool(
                "busybox_unzip", archive,
                ["busybox", "unzip", "-o", "-q", "{archive}", "-d", "{output}"]
            ))
        
        # Cleanup test archive
        archive.unlink()
        
        # Sort by speed
        successful = [r for r in results if r["success"]]
        successful.sort(key=lambda x: x["speed_mbps"], reverse=True)
        
        # Print summary
        logger.info("\n" + "=" * 60)
        logger.info("RESULTS SUMMARY (sorted by speed)")
        logger.info("=" * 60)
        logger.info(f"{'Tool':<25} {'Time':>10} {'Speed':>12} {'Status':>10}")
        logger.info("-" * 60)
        
        for r in results:
            status = "✅ OK" if r["success"] else "❌ FAIL"
            time_str = format_time(r["duration"]) if r["success"] else "-"
            speed_str = f"{r['speed_mbps']:.1f} MB/s" if r["success"] else "-"
            logger.info(f"{r['tool']:<25} {time_str:>10} {speed_str:>12} {status:>10}")
        
        if successful:
            best = successful[0]
            logger.info("-" * 60)
            logger.info(f"🏆 FASTEST: {best['tool']} at {best['speed_mbps']:.1f} MB/s")
        
        return results


class GOGDLBinaryTester:
    """Test gogdl as a CLI binary"""
    
    def __init__(self, credentials_path: str, test_dir: str):
        self.credentials_path = Path(credentials_path)
        self.test_dir = Path(test_dir)
        self.test_dir.mkdir(parents=True, exist_ok=True)
        
    def find_gogdl_binary(self) -> Optional[str]:
        """Find gogdl binary or script"""
        locations = [
            # System installed
            "gogdl",
            # Local bin
            "/home/deck/.local/bin/gogdl",
            # Heroic's gogdl
            "/home/deck/.var/app/com.heroicgameslauncher.hgl/config/heroic/tools/gogdl/bin/gogdl",
            # Our project
            str(Path(__file__).parent.parent / "heroic-gogdl-main" / "gogdl" / "cli.py"),
        ]
        
        for loc in locations:
            if loc.endswith(".py"):
                if Path(loc).exists():
                    return f"python3 {loc}"
            else:
                result = subprocess.run(["which", loc], capture_output=True)
                if result.returncode == 0:
                    return loc
        
        return None
    
    async def test_gogdl_info(self, game_id: str) -> dict:
        """Test gogdl info command"""
        result = {
            "command": "info",
            "success": False,
            "output": "",
            "error": "",
            "duration": 0
        }
        
        gogdl = self.find_gogdl_binary()
        if not gogdl:
            result["error"] = "gogdl binary not found"
            return result
        
        try:
            cmd = f"{gogdl} info {game_id} --auth-config-path {self.credentials_path}"
            
            start = time.time()
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            duration = time.time() - start
            
            result["duration"] = duration
            result["output"] = stdout.decode()[:500]
            
            if proc.returncode == 0:
                result["success"] = True
                try:
                    result["parsed"] = json.loads(stdout.decode())
                except:
                    pass
            else:
                result["error"] = stderr.decode()[:200]
                
        except Exception as e:
            result["error"] = str(e)
        
        return result
    
    async def test_gogdl_download(self, game_id: str, platform: str = "linux") -> dict:
        """Test gogdl download command"""
        result = {
            "command": "download",
            "success": False,
            "duration": 0,
            "files": 0,
            "size": 0,
            "speed_mbps": 0,
            "error": ""
        }
        
        gogdl = self.find_gogdl_binary()
        if not gogdl:
            result["error"] = "gogdl binary not found"
            return result
        
        install_path = self.test_dir / f"gogdl_test_{game_id}"
        install_path.mkdir(parents=True, exist_ok=True)
        
        try:
            cmd = (f"{gogdl} download {game_id} "
                   f"--platform {platform} "
                   f"--path {install_path} "
                   f"--auth-config-path {self.credentials_path} "
                   f"--skip-dlcs")
            
            logger.info(f"Running: {cmd[:100]}...")
            
            start = time.time()
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Stream output
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                logger.info(f"  {line.decode().strip()}")
            
            await proc.wait()
            duration = time.time() - start
            
            result["duration"] = duration
            
            if proc.returncode == 0:
                # Count files
                files = list(install_path.rglob("*"))
                file_count = len([f for f in files if f.is_file()])
                total_bytes = sum(f.stat().st_size for f in files if f.is_file())
                
                result["success"] = True
                result["files"] = file_count
                result["size"] = total_bytes
                result["speed_mbps"] = (total_bytes / (1024*1024)) / duration if duration > 0 else 0
                
                logger.info(f"✅ Downloaded {file_count} files, {format_size(total_bytes)} in {format_time(duration)}")
            else:
                stderr = await proc.stderr.read()
                result["error"] = stderr.decode()[:200]
                logger.info(f"❌ Failed: {result['error']}")
                
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"Error: {e}")
        finally:
            # Cleanup
            if install_path.exists():
                shutil.rmtree(install_path, ignore_errors=True)
        
        return result


async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Test extraction tools and gogdl")
    parser.add_argument("--test-dir", 
                       default=os.path.expanduser("~/.local/share/unifideck/extract_test"),
                       help="Test directory")
    parser.add_argument("--archive-size", type=int, default=100,
                       help="Test archive size in MB")
    parser.add_argument("--skip-extraction", action="store_true",
                       help="Skip extraction tool tests")
    parser.add_argument("--skip-gogdl", action="store_true",
                       help="Skip gogdl tests")
    parser.add_argument("--gogdl-game", default="1097893768",
                       help="GOG game ID to test download (default: small game)")
    parser.add_argument("--credentials",
                       default=os.path.expanduser("~/.config/unifideck/gog_credentials.json"),
                       help="GOG credentials path")
    
    args = parser.parse_args()
    
    all_results = {}
    
    # Test extraction tools
    if not args.skip_extraction:
        extractor = ExtractionTester(args.test_dir)
        all_results["extraction"] = await extractor.run_all_tests(args.archive_size)
    
    # Test gogdl
    if not args.skip_gogdl:
        logger.info("\n" + "=" * 60)
        logger.info("GOGDL BINARY TEST")
        logger.info("=" * 60)
        
        gogdl_tester = GOGDLBinaryTester(args.credentials, args.test_dir)
        
        gogdl_binary = gogdl_tester.find_gogdl_binary()
        if gogdl_binary:
            logger.info(f"Found gogdl: {gogdl_binary}")
            
            # Test info command
            info_result = await gogdl_tester.test_gogdl_info(args.gogdl_game)
            all_results["gogdl_info"] = info_result
            
            if info_result["success"]:
                logger.info(f"✅ gogdl info: {info_result['duration']:.1f}s")
                
                # Only download if info worked
                # download_result = await gogdl_tester.test_gogdl_download(args.gogdl_game)
                # all_results["gogdl_download"] = download_result
            else:
                logger.info(f"❌ gogdl info failed: {info_result['error']}")
        else:
            logger.info("❌ gogdl binary not found")
            all_results["gogdl"] = {"error": "not found"}
    
    # Save results
    results_file = Path(args.test_dir) / "results.json"
    with open(results_file, 'w') as f:
        json.dump(all_results, f, indent=2, default=str)
    logger.info(f"\nResults saved to: {results_file}")
    
    return all_results


if __name__ == "__main__":
    asyncio.run(main())
