"""Typed reads out of one store's config section.

Every store that builds a frozen config snapshot from ``ConfigManager`` needs
the same three coercions — string, int, list-of-strings — under its own
``stores.<name>`` prefix, each tolerating a missing key and a wrong type,
because the config file is user-editable and a typo there must not take the
plugin down.

GOG and Microsoft each defined all three as nested closures inside their
``from_config_manager``, byte-identical apart from the prefix constant they
captured. Check 13 caught ``_list``; its ``_s`` and ``_i`` siblings sat just
under the body-size floor and would have gone unnoticed — a reminder that the
floor buys precision at the cost of the smallest duplicates (audit register
item 47).

The prefix is a constructor argument rather than a module-level lookup, so
this stays usable by any store — including the two wrapper stores, whose
config classes read the same shapes through their own ``_FIELD_SPECS``
tables and are deliberately left alone here: those declare *which* keys
exist, which is a different job from coercing one.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from unifideck.utils.config_helpers import get_cfg

if TYPE_CHECKING:
    from unifideck.config import ConfigManager


class StoreConfigReader:
    """Read ``stores.<store>.<key>`` with a forgiving type coercion.

    Args:
        config: the live manager, or ``None`` — every read then returns its
            default, which is what lets a store construct before config is
            available (the launcher's reduced service graph does exactly
            that).
        prefix: the dotted section, e.g. ``"stores.gog"``.
    """

    __slots__ = ("_config", "_prefix")

    def __init__(self, config: ConfigManager | None, prefix: str) -> None:
        self._config = config
        self._prefix = prefix

    def _raw(self, key: str, default: Any) -> Any:
        return get_cfg(self._config, f"{self._prefix}.{key}", default)

    def text(self, key: str, default: str = "") -> str:
        """A stripped string. ``None`` yields *default*, not ``"None"``."""
        val = self._raw(key, default)
        return str(val).strip() if val is not None else default

    def number(self, key: str, default: int) -> int:
        """An int, falling back to *default* on anything uncoercible."""
        val = self._raw(key, default)
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def text_list(self, key: str) -> list[str]:
        """A list of non-empty strings; anything else yields ``[]``.

        Non-string members are dropped rather than coerced: a list that is
        half URLs and half numbers is a malformed config, and inventing
        ``"3"`` as a redirect URI would be worse than ignoring it.
        """
        val = self._raw(key, None)
        if not isinstance(val, list):
            return []
        return [str(x) for x in val if isinstance(x, str) and x]
