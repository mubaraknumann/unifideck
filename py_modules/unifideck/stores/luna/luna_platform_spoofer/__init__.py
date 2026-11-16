"""Luna platform spoofer: browser-level CDP OS-identity override.

This package replaces the former single ``luna_platform_spoofer.py`` module
(it outgrew the 550-LOC volumetry cap). It is split by responsibility:

* :mod:`.payloads` — pure, browser-free construction: version parsing, the
  :class:`SpoofIdentity`, the CDP override payloads and the platform-probe
  extraction (Req 14.3-14.9, 12.6).
* :mod:`.session` — the one I/O seam: the :class:`BrowserCdpSession` protocol
  and its aiohttp implementation.
* :mod:`.spoofer` — :class:`LunaPlatformSpoofer`, the browser-level
  auto-attach + per-target override orchestration and the ``verify`` probe.

The public surface is re-exported here, so every historical import path
(``from unifideck.stores.luna.luna_platform_spoofer import <name>`` and
``import unifideck.stores.luna.luna_platform_spoofer as mod``) keeps working
unchanged.
"""
from __future__ import annotations

from unifideck.stores.luna.luna_platform_spoofer.payloads import (
    LunaSpoofConfigError,
    SpoofIdentity,
    build_platform_override_script,
    build_set_user_agent_override_params,
    build_spoof_identity,
    build_user_agent_metadata,
    derive_browser_version,
    extract_platform_from_evaluate,
    parse_browser_version,
)
from unifideck.stores.luna.luna_platform_spoofer.session import (
    BrowserCdpSession,
    _AiohttpBrowserCdpSession,
)
from unifideck.stores.luna.luna_platform_spoofer.spoofer import (
    LunaPlatformSpoofer,
)

__all__ = [
    "BrowserCdpSession",
    "LunaPlatformSpoofer",
    "LunaSpoofConfigError",
    "SpoofIdentity",
    "_AiohttpBrowserCdpSession",
    "build_platform_override_script",
    "build_set_user_agent_override_params",
    "build_spoof_identity",
    "build_user_agent_metadata",
    "derive_browser_version",
    "extract_platform_from_evaluate",
    "parse_browser_version",
]
