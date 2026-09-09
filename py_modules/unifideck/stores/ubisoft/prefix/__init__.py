"""
Prefix sub-package — public exports.

Re-exports ``UbisoftPrefixManager``, the orchestration class for Wine
prefix lifecycle: create, template, mount, validate, remove.
"""

from .manager import UbisoftPrefixManager

__all__ = ["UbisoftPrefixManager"]
