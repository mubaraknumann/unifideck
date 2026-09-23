"""auth.edge_browser — Microsoft Edge lifecycle subpackage.

Manages the Microsoft Edge browser process used for OAuth auth
flows (Epic, GOG, Amazon, Microsoft) and xCloud game streaming.
Previously a flat 753 LOC module ``auth/edge_browser.py`` plus
3 extracted helper modules (``edge_cdp_client``, ``edge_installer``,
``edge_profile``); reorganised on 2026-04-18 into this subpackage:

  - ``edge``       : EdgeBrowser façade class
  - ``env``        : session env detection pipeline
  - ``launch``     : launch_auth / launch_browser_game helpers
  - ``cdp_client`` : CDP traffic (HTTP + WebSocket)
  - ``installer``  : flatpak install + udev override
  - ``detection``  : install detection helpers (pure functions)
  - ``profile``    : profile dir + cookie management

Public API preserved via re-export: callers continue to use
``from unifideck.auth.edge_browser import EdgeBrowser``.
"""
from __future__ import annotations

from .edge import EdgeBrowser

__all__ = ["EdgeBrowser"]
