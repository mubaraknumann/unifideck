"""Battle.net ownership and installed-state — public surface.

py_modules/unifideck/stores/battlenet/ownership/__init__.py

The playable catalog is not a list Blizzard hands out; it is the result of
evaluating the PUB catalog's rules against account facts:

  ``licenses.py``    licence ids from ``CachedData.db`` (plain SQLite)
  ``pub_catalog.py`` merges the cached PUB catalog fragments (plain JSON)
  ``rules.py``       evaluates the rules -> the products actually granted
  ``installed.py``   ``aggregate.json`` (+ ``product.db``) for install state

Licences alone miss every free-to-play and subscription title, because those
match on ``game_account`` rather than ``license_id``, and nothing the client
writes locally records which game accounts the user holds. The caller
(``library.grant_ownership``) closes that by presuming one for every catalog
program; this package only assembles and evaluates, never decides policy.
"""

from .installed import (
    InstalledGame,
    merge_install_state,
    parse_aggregate,
    read_aggregate,
    read_installed,
    resolve_host_paths,
)
from .licenses import AccountLicences, parse_licences, read_licences
from .pub_catalog import (
    CatalogEntry,
    MergedCatalog,
    load_catalog,
    merge_fragments,
    read_catalog,
)
from .rules import (
    AccountFacts,
    GrantedProduct,
    evaluate_catalog,
    evaluate_program,
    matches,
)

__all__ = [
    "AccountFacts",
    "AccountLicences",
    "CatalogEntry",
    "GrantedProduct",
    "InstalledGame",
    "MergedCatalog",
    "evaluate_catalog",
    "evaluate_program",
    "load_catalog",
    "matches",
    "merge_fragments",
    "merge_install_state",
    "parse_aggregate",
    "parse_licences",
    "read_aggregate",
    "read_catalog",
    "read_installed",
    "read_licences",
    "resolve_host_paths",
]
