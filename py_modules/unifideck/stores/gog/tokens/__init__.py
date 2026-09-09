"""Tokens sub-package — public exports.

Re-exports ``GOGTokenManager``, the orchestration class for
GOG OAuth token lifecycle (load, refresh, save, clear), and
``ExchangeOutcome`` — the three-state result of a code exchange that
the auth facade maps to distinct error codes.
"""

from .manager import GOGTokenManager
from .oauth import ExchangeOutcome
from .user_info import GOGUserInfo

__all__ = ["ExchangeOutcome", "GOGTokenManager", "GOGUserInfo"]
