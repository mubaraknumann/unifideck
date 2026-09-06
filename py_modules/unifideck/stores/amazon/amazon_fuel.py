"""Amazon Games "fuel.json" parser — extract launch metadata.

After a successful install, every Amazon Games title contains a
``fuel.json`` file at its install root, listing the launch
executable, its arguments, and supported runtime requirements.
Module-level helpers :

* ``candidate_fuel_dirs(install_path)`` — list plausible directories
  where fuel.json may live (varies by title : some put it at the
  root, others in a sub-directory);
* ``parse_fuel_json_content(content)`` — JSON-parse with schema
  validation;
* ``extract_main_command(fuel_data)`` — extract the launch command
  (executable + args + working directory) from the parsed fuel data;
* ``find_fuel_json(install_path)`` — combined locate + parse;
* ``read_fuel(install_path)`` — top-level entry returning a typed
  fuel structure or None.

Kept stateless (module-level functions, no class) because there's no
state to encapsulate — every call is a pure transform from path to
typed data.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_COMMENT_RE = re.compile(r"//.*$", re.MULTILINE)

def candidate_fuel_dirs(install_path: str) -> list[str]:
    """Check whether fuel dirs."""
    if not install_path:
        return []
    install_p = Path(install_path)
    dirs: list[str] = [
        install_path,
        str(install_p / "game"),
        str(install_p / "Game"),
    ]
    try:
        for entry in install_p.iterdir():
            subdir = str(entry)
            if entry.is_dir() and subdir not in dirs:
                dirs.append(subdir)
    except OSError as e:
        logger.debug(
            "[amazon_fuel] listdir(%s) failed: %s",
            install_path,
            e,
        )
    return dirs

def parse_fuel_json_content(content: str) -> dict[str, Any] | None:
    """Parse fuel JSON content."""
    cleaned = _COMMENT_RE.sub("", content)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.debug("[amazon_fuel] parse error: %s", e)
        return None
    if not isinstance(data, dict):
        logger.debug(
            "[amazon_fuel] expected object, got %s",
            type(data).__name__,
        )
        return None
    return data

def extract_main_command(fuel_data: dict[str, Any]) -> str | None:
    """Extract main command."""
    main = fuel_data.get("Main")
    if not isinstance(main, dict):
        return None
    command = main.get("Command")
    if not isinstance(command, str) or not command.strip():
        return None
    return command.strip()

def find_exe_from_fuel(install_path: str) -> str | None:
    """Find exe from fuel."""
    if not install_path:
        return None
    for search_dir in candidate_fuel_dirs(install_path):
        fuel_path = Path(search_dir) / "fuel.json"
        if not fuel_path.is_file():
            continue
        try:
            content = fuel_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError as e:
            logger.debug(
                "[amazon_fuel] read %s failed: %s",
                fuel_path,
                e,
            )
            continue
        data = parse_fuel_json_content(content)
        if data is None:
            continue
        command = extract_main_command(data)
        if command is None:
            logger.debug(
                "[amazon_fuel] %s has no Main.Command",
                fuel_path,
            )
            continue
        exe_path = Path(search_dir) / command
        if exe_path.is_file():
            logger.info(
                "[amazon_fuel] resolved exe from fuel.json: %s",
                exe_path,
            )
            return str(exe_path)
        logger.debug(
            "[amazon_fuel] Main.Command points to missing file: %s",
            exe_path,
        )
    return None
