"""What a Microsoft account owns (not just what it can stream).

The xCloud library (``microsoft_catalog``) is the titles the account may
stream: Game Pass plus owned games Microsoft offers in the cloud. Ownership
is a different list: every purchase, PC or console, streamable or not. It
feeds the Steam Store "already owned elsewhere" ribbon and nothing else; it
never creates library rows or shortcuts.
"""
from __future__ import annotations

from .reader import MicrosoftOwnershipReader, OwnedFetch, OwnedProduct

__all__ = ["MicrosoftOwnershipReader", "OwnedFetch", "OwnedProduct"]
