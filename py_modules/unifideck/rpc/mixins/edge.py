"""EdgeRPCMixin — install the plugin's browser.

Browser sign-ins (Epic / GOG / Amazon / Microsoft / Ubisoft / itch.io), store
windows and streams open in the plugin's own Chromium build
(``auth/edge_browser/chrome_installer``), which replaced the Microsoft Edge
flatpak in 0.8.0. It is downloaded in the background at startup; this RPC is
the on-demand path when it is still missing.

Sign-ins detect it **reactively**: the browser stores return a structured
``error="edge_not_installed"`` from ``store_auth``, and ``useStoreAuth``
spawns ``ChromiumInstallModal`` on it. Store windows cannot report back, so
the cart button asks ``browser_installed`` first. The names (``edge_*``,
``install_edge``) are kept from the Edge era so the frontend contract stays
stable.

It proxies through the Microsoft store because that's where the
``EdgeBrowser`` singleton lives; all stores share the one browser. If the
Microsoft store hasn't been registered (test installs, opted-out builds) the
mixin returns a structured non-success response rather than raising — the
frontend treats ``unavailable`` as "show the modal".
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

class EdgeRPCMixin:
    """The plugin's browser: install + check."""

    registry: Any

    async def install_edge(self) -> Any:
        """Download the plugin's browser via the Microsoft store's helper.

        Wraps ``MicrosoftStore.install_edge``. The download is about
        160 MB, so it can take a few minutes; the frontend modal shows a
        spinner during the call.

        Returns:
            ``{installed: bool, error: str | None}`` as the
            data payload. Returning a dict that does **not**
            start with a ``success`` key keeps ``_to_envelope``
            from treating this as a caller-supplied envelope
            (which would collapse ``data`` to ``None`` and
            break frontend unwrapping). ``error`` is populated
            on failure (``"edge_browser_not_configured"``, or an
            i18n key such as ``"microsoft.browserInstallFailed"``).
        """
        store = self.registry.get("microsoft")
        if store is None or not hasattr(store, "install_edge"):
            logger.warning(
                "[EdgeRPC] install_edge unavailable "
                "(microsoft store missing)",
            )
            return {
                "installed": False,
                "error": "microsoft_store_unavailable",
            }
        logger.info("[EdgeRPC] install_edge: downloading the plugin's browser")
        result = await store.install_edge()
        success = bool(getattr(result, "success", False))
        error = getattr(result, "error", None)
        logger.info(
            "[EdgeRPC] install_edge result: success=%s error=%s",
            success, error,
        )
        return {"installed": success, "error": error}

    async def browser_installed(self) -> Any:
        """Whether the plugin's browser is downloaded: ``{installed: bool}``.

        The one pre-check: a store's shop window opens through a Steam
        shortcut, so a missing browser there could not be reported back to
        the cart button, and the window would simply never appear.
        """
        store = self.registry.get("microsoft")
        installed = bool(
            store is not None
            and hasattr(store, "is_edge_installed")
            and store.is_edge_installed(),
        )
        return {"installed": installed}
