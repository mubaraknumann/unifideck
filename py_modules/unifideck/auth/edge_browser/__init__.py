"""auth.edge_browser — the plugin's browser lifecycle subpackage.

Manages the browser process used for store sign-ins (Epic, GOG, Amazon,
Microsoft, Ubisoft, itch.io), storefront windows and browser games. Since
0.7.7 that browser is the plugin's own Chrome build, downloaded by
``chrome_installer``; before that it was the Microsoft Edge flatpak, which is
where the package and class names come from.

  - ``edge``             : EdgeBrowser façade class
  - ``chrome_installer`` : download + verify the pinned Chrome build
  - ``env``              : session env detection pipeline
  - ``launch``           : launch_auth / launch_browser_game helpers
  - ``cdp_client``       : CDP traffic (HTTP + WebSocket)
  - ``profile``          : profile dir + cookie management

Public API preserved via re-export: callers continue to use
``from unifideck.auth.edge_browser import EdgeBrowser``.
"""
from __future__ import annotations

from .edge import EdgeBrowser

__all__ = ["EdgeBrowser"]
