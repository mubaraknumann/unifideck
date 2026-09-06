"""cdp — Chrome DevTools Protocol clients for the Steam/Edge CEF endpoints.

Three independent modules, each imported by full path rather than through
this package (so there are deliberately no re-exports here):

* ``cdp_client``          — :class:`CDPClient`, the shared async websocket
  client. Built by ``services/bootstrap`` and used by ``auth/browser``.
* ``page_inject``         — target listing + script injection primitives,
  consumed by ``launcher/cdp``.
* ``xcloud_browser_shims`` — the xCloud gamepad/WSI shim JS, consumed by
  ``launcher/cdp/xcloud_cdp``.

``cdp_inject`` (``SteamCSSInjector``, ``get_cdp_client``,
``shutdown_cdp_client``, ``build_marker_id``) was deleted in the audit
§1.2 pass: its only reachable caller was the ``inject_hide_css`` RPC,
which had no frontend caller — the frontend hides Steam UI with its own
scoped-CSS marker instead. The ``create_cef_debugging_flag`` re-export
went with it; its ``cdp_utils`` module has never existed on disk, so the
guarded import resolved to ``None`` on every run.
"""

from __future__ import annotations

__all__: list[str] = []
