"""The "is this a usable install directory?" rule, in one place.

Five of the six stores override :meth:`StoreBase.get_installed_path` so
App-Details can measure the real "Installed size" when the sync cache's
``install_path`` is missing or stale. Four of those overrides ended with the
same three lines: pull a key out of the store's installed-games record, and
return it only if it is a non-empty string.

GOG's and Ubisoft's bodies were **byte-identical** apart from the docstring;
Amazon's was the same shape reading a different key.

What is shared here is the *guard*, not the fetch. The fetch genuinely differs
per store — GOG and Ubisoft call a synchronous library scan in a thread,
Amazon awaits a whole map and indexes it, Epic reads legendary's
``installed.json`` through its own reader, and Battle.net resolves through its
id-map and the client's install records. Forcing one signature over all of
those would be the over-generalisation audit §3.2 warns about, where the
majority implementation is not automatically the right one.

The guard is worth centralising because it is precisely where a live defect
was found: §3.4 records that Amazon's ``read_installed_ids`` defaults a
missing path to ``""``, and a falsy-but-present path flowed through as though
it were real, marking a game installed with nowhere to measure. ``""`` is not
a directory, and exactly one function should have to know that.
"""
from __future__ import annotations

from typing import Any

#: The key GOG and Ubisoft use in their installed-games records. Amazon's
#: nile records call the same thing ``path``, which is the only reason that
#: store passes ``key=``.
DEFAULT_PATH_KEY = "install_path"


def install_path_from_record(
    record: Any,
    *,
    key: str = DEFAULT_PATH_KEY,
) -> str | None:
    """The install directory named in *record*, or ``None``.

    Args:
        record: a store's per-game installed record. Anything that is not a
            mapping yields ``None`` rather than raising — a malformed
            ``user.json`` or ``installed.json`` is a real case (§3.2 found
            Amazon raising ``AttributeError`` out of the store-status path
            when the file held a JSON array), and a store-status refresh
            must not blow up on one.
        key: which field holds the path. Defaults to ``install_path``.

    Returns:
        The path, or ``None`` when the record is unusable — absent, not a
        mapping, missing the key, holding a non-string, or holding an empty
        string. The empty-string case is the one that mattered in practice.
    """
    if not isinstance(record, dict):
        return None
    value = record.get(key)
    if isinstance(value, str) and value:
        return value
    return None
