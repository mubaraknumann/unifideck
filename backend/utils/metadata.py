"""Metadata utilities for game information formatting and conversion.

Provides helpers for:
- Sanitizing descriptions from various sources (RAWG, Steam)
- Converting appinfo.vdf format to Steam Web API format
- Extracting metadata from appinfo data by title matching
"""

import re
import logging
from typing import Dict, List, Tuple, Any

logger = logging.getLogger(__name__)


def sanitize_description(text: str, max_length: int = 1000) -> str:
    """Clean up RAWG/Steam descriptions for display.

    Strips markdown headers, HTML tags, fixes missing spaces, and normalizes whitespace.

    Args:
        text: Raw description text from API
        max_length: Maximum length to truncate to

    Returns:
        Cleaned description text
    """
    if not text:
        return ""

    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Fix markdown headers with missing space (e.g. "###Plot" -> "Plot")
    # Also strips the header markers entirely since we just want plain text
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    # Remove leftover markdown bold/italic markers
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    # Fix sentences joined without space (e.g. "end.Start" -> "end. Start")
    text = re.sub(r"([.!?])([A-Z])", r"\1 \2", text)
    # Normalize whitespace: collapse multiple spaces/newlines into single space
    text = re.sub(r"\s+", " ", text).strip()
    # Truncate to max length
    if len(text) > max_length:
        text = text[:max_length].rsplit(" ", 1)[0] + "..."
    return text


def build_appinfo_entry(steam_app_id: int, metadata: Dict) -> Dict:
    """Build an appinfo.vdf entry from cached metadata.

    Args:
        steam_app_id: Steam app ID to use in the entry
        metadata: Metadata dict (typically from Steam Web API format)

    Returns:
        Dict in appinfo.vdf format
    """
    platforms = metadata.get("platforms", {})
    os_list = []
    if platforms.get("windows"):
        os_list.append("windows")
    if platforms.get("mac"):
        os_list.append("macos")
    if platforms.get("linux"):
        os_list.append("linux")

    developers = metadata.get("developers", [])
    publishers = metadata.get("publishers", [])

    return {
        "appinfo": {
            "appid": steam_app_id,
            "common": {
                "name": metadata.get("name", "Unknown"),
                "type": "game",
                "oslist": ",".join(os_list) if os_list else "windows",
                "controller_support": metadata.get("controller_support", "none"),
                "metacritic_score": metadata.get("metacritic", {}).get("score", 0),
            },
            "extended": {
                "developer": ", ".join(developers) if developers else "",
                "publisher": ", ".join(publishers) if publishers else "",
                "homepage": metadata.get("website") or "",
            },
        }
    }


def convert_appinfo_to_web_api_format(app_id: int, appinfo: Dict) -> Dict:
    """Convert appinfo.vdf format to Steam web API format for compatibility with frontend.

    Args:
        app_id: Steam app ID
        appinfo: Data from appinfo.vdf in parsed dict format

    Returns:
        Dict in Steam Web API format (compatible with storefront API responses)
    """
    try:
        common = appinfo.get("appinfo", {}).get("common", {})
        extended = appinfo.get("appinfo", {}).get("extended", {})

        # Extract developer/publisher (can be string or list)
        developer = extended.get("developer", "")
        developers = (
            developer.split(",")
            if isinstance(developer, str) and developer
            else (developer if isinstance(developer, list) else [])
        )

        publisher = extended.get("publisher", "")
        publishers = (
            publisher.split(",")
            if isinstance(publisher, str) and publisher
            else (publisher if isinstance(publisher, list) else [])
        )

        return {
            "type": common.get("type", "game"),
            "name": common.get("name", ""),
            "steam_appid": app_id,
            "required_age": common.get("required_age", 0),
            "is_free": common.get("is_free", False),
            "controller_support": common.get("controller_support", "none"),
            "detailed_description": extended.get("description", ""),
            "short_description": common.get("short_description", ""),
            "supported_languages": common.get("languages", ""),
            "header_image": (
                common.get("header_image", {}).get("english")
                if isinstance(common.get("header_image"), dict)
                else common.get("header_image", "")
            ),
            "capsule_image": (
                common.get("library_assets", {}).get("library_capsule", "")
                if isinstance(common.get("library_assets"), dict)
                else ""
            ),
            "website": extended.get("homepage", ""),
            "developers": [d.strip() for d in developers if d.strip()],
            "publishers": [p.strip() for p in publishers if p.strip()],
            "platforms": {
                "windows": "oslist" in common
                and "windows" in str(common.get("oslist", "")).lower(),
                "mac": "oslist" in common
                and "macos" in str(common.get("oslist", "")).lower(),
                "linux": "oslist" in common
                and "linux" in str(common.get("oslist", "")).lower(),
            },
            "metacritic": {"score": common.get("metacritic_score", 0)},
            "categories": (
                common.get("category", {})
                if isinstance(common.get("category"), dict)
                else []
            ),
            "genres": (
                common.get("genre", {}) if isinstance(common.get("genre"), dict) else []
            ),
            "release_date": {
                "coming_soon": False,
                "date": str(common.get("steam_release_date", "")),
            },
        }
    except Exception as e:
        logger.error(f"Error converting appinfo for {app_id}: {e}")
        return {}


async def extract_metadata_from_appinfo(
    games: List[Any], appinfo_data: Dict[int, Dict]
) -> Tuple[Dict[int, int], Dict[int, Dict]]:
    """Extract metadata for our games from appinfo data by matching titles.

    Searches appinfo.vdf for Steam apps matching game titles, then converts
    metadata to web API format for caching.

    Args:
        games: List of Game objects with app_id and title
        appinfo_data: Parsed appinfo.vdf data (app_id -> sections dict)

    Returns:
        Tuple of (shortcut_appid_to_steam_appid mapping, steam_appid_to_metadata mapping)
    """
    appid_mapping = {}  # shortcut_appid -> steam_appid
    metadata_results = {}  # steam_appid -> metadata (converted to web API format)

    for game in games:
        if not game.app_id or not game.title:
            continue

        try:
            # Search for Steam App ID by title in appinfo data
            search_lower = game.title.lower().strip()
            steam_app_id = None

            for app_id, app_data in appinfo_data.items():
                try:
                    # Get app name from common section
                    app_common = app_data.get("appinfo", {}).get("common", {})
                    app_name = app_common.get("name", "").lower().strip()

                    if app_name == search_lower:
                        steam_app_id = app_id
                        break
                except:
                    continue

            if not steam_app_id:
                continue

            appid_mapping[game.app_id] = steam_app_id

            # Convert appinfo data to web API format
            if steam_app_id not in metadata_results:
                converted = convert_appinfo_to_web_api_format(
                    steam_app_id, appinfo_data[steam_app_id]
                )
                if converted:
                    metadata_results[steam_app_id] = converted
                    logger.debug(
                        f"Extracted metadata for '{game.title}' (Steam ID: {steam_app_id})"
                    )

        except Exception as e:
            logger.debug(f"Failed to extract metadata for '{game.title}': {e}")

    return appid_mapping, metadata_results
