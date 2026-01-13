"""
Launch Options Parser - Resilient Store ID Extraction

This module provides utilities for extracting store:game_id patterns from Steam
shortcut LaunchOptions, even when additional parameters (like LSFG, MANGOHUD, etc.)
are present before or after the ID.

Pattern: (epic|gog|amazon):([a-zA-Z0-9][a-zA-Z0-9._-]*)

Examples:
    - "epic:4141431341" -> ("epic", "4141431341")
    - "MANGOHUD=1 epic:abc123 --no-splash" -> ("epic", "abc123")
    - "gog:1234567890" -> ("gog", "1234567890")
    - "amazon:amzn1.adg.product.8a584db6-f8e5-4bfa-87a8-256a9d3506c4"
        -> ("amazon", "amzn1.adg.product.8a584db6-f8e5-4bfa-87a8-256a9d3506c4")
"""

import re
from typing import Optional, Tuple

# Regex pattern to match store:game_id anywhere in the string
# Amazon game IDs contain dots (e.g., amzn1.adg.product.xxx), so we include '.' in the pattern
# Note: Using word boundary at end won't work with trailing dots, so we use a non-greedy match
# and require the ID to start with alphanumeric
STORE_ID_PATTERN = re.compile(r'\b(epic|gog|amazon):([a-zA-Z0-9][a-zA-Z0-9._-]*)')


def extract_store_id(launch_options: str) -> Optional[Tuple[str, str]]:
    """
    Extract store and game_id from launch options containing additional text.
    
    This is the primary function for parsing LaunchOptions. It handles cases where
    users have added LSFG parameters, environment variables, or other flags.
    
    Args:
        launch_options: Full LaunchOptions string (may contain extra parameters)
        
    Returns:
        Tuple of (store, game_id) or None if no valid pattern found
        
    Examples:
        >>> extract_store_id("epic:4141431341")
        ('epic', '4141431341')
        >>> extract_store_id("MANGOHUD=1 epic:abc123 --no-splash")
        ('epic', 'abc123')
        >>> extract_store_id("--some-random-option")
        None
    """
    if not launch_options:
        return None
    
    match = STORE_ID_PATTERN.search(launch_options)
    if match:
        return (match.group(1), match.group(2))
    return None


def is_unifideck_shortcut(launch_options: str) -> bool:
    """
    Check if launch options contain a Unifideck store:id pattern.
    
    Use this for quickly determining if a shortcut is managed by Unifideck
    without needing to extract the full ID.
    
    Args:
        launch_options: LaunchOptions string to check
        
    Returns:
        True if the string contains a valid store:id pattern
        
    Examples:
        >>> is_unifideck_shortcut("epic:game123")
        True
        >>> is_unifideck_shortcut("LSFG=1 gog:12345 --option")
        True
        >>> is_unifideck_shortcut("--custom-options")
        False
    """
    if not launch_options:
        return False
    return STORE_ID_PATTERN.search(launch_options) is not None


def get_store_prefix(launch_options: str) -> Optional[str]:
    """
    Get just the store name (epic/gog/amazon) if present.
    
    Args:
        launch_options: LaunchOptions string to parse
        
    Returns:
        Store name string or None if not found
        
    Examples:
        >>> get_store_prefix("MANGOHUD=1 gog:12345")
        'gog'
        >>> get_store_prefix("--no-store-here")
        None
    """
    if not launch_options:
        return None
    
    match = STORE_ID_PATTERN.search(launch_options)
    return match.group(1) if match else None


def get_full_id(launch_options: str) -> Optional[str]:
    """
    Get the complete store:game_id string for use as a key.
    
    This reconstructs the canonical store:id format from any launch options string,
    regardless of surrounding text. Useful for map lookups and comparisons.
    
    Args:
        launch_options: LaunchOptions string to parse
        
    Returns:
        Canonical "store:game_id" string or None if not found
        
    Examples:
        >>> get_full_id("PROTON_LOG=1 epic:abc123 --skip-launcher")
        'epic:abc123'
        >>> get_full_id("gog:1234567890")
        'gog:1234567890'
        >>> get_full_id("random text")
        None
    """
    if not launch_options:
        return None
    
    match = STORE_ID_PATTERN.search(launch_options)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    return None
