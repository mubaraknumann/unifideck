"""Authenticated GOG user info — small frozen dataclass.

``GOGUserInfo`` is a frozen dataclass with ``username`` and
``galaxy_user_id``. Used as the public face of the authenticated user
for the UI (avatar, displayed name) and stored alongside tokens for
display after restart without needing to query GOG.com.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GOGUserInfo:
    """Goguser info."""

    username: str = ""
    galaxy_user_id: str = ""
