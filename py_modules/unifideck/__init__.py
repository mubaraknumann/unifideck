"""Unifideck — unified game library for Steam Deck.

Top-level package. Defines the public API of the plugin's backend,
accessible from any Decky-loaded code via simple
`from unifideck.X import Y` imports.

The layered architecture is documented in ``docs/architecture.md``; the
layer diagram there is authoritative (do not restate a layer count here).
Imports flow downward only.

Adjacent packages (`auth/`, `cdp/`, `compatibility/`, `metadata/`,
`steam/`, `utils/`) provide support modules.

No ``__version__`` here on purpose. This package used to carry one, it
had no reader anywhere in the tree, and it sat two releases behind
``package.json`` before anyone noticed (audit §2.6). The version is
read from ``package.json`` by the updater
(``services/updater/service.py``) and collected from it by the support
bundle, which labels it the source of truth; ``build-plugin.sh`` parses
the same file. Add a reader there, not a second copy here.
"""
from __future__ import annotations
