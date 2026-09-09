"""EdgeRPCMixin — Microsoft Edge install + readiness RPCs.

Browser-based store auth (Epic / GOG / Amazon / Microsoft)
opens the OAuth URL inside a Wine-prefixed instance of
Microsoft Edge (flatpak ``com.microsoft.Edge``). The
prerequisite isn't always installed on a fresh Steam Deck, so
the frontend needs a way to trigger an install via flatpak
when it's missing, with a spinner + result toast.

Detection is **reactive, not a pre-check**: all four
browser-auth stores return a structured
``error="edge_not_installed"`` from ``store_auth``, and
``useStoreAuth`` spawns ``ChromiumInstallModal`` on it. A
matching ``is_edge_installed`` RPC also existed here but had no
frontend caller and was deleted in the audit §1.2 pass — the
reactive path already covers the case, and a pre-check would
have raced it. ``detection.is_edge_installed`` and
``MicrosoftStore.is_edge_installed`` are untouched; the
installer still uses them.

It proxies through the Microsoft store because that's where the
``EdgeBrowser`` singleton lives — all stores share the same
flatpak install so reaching it from one store is enough.

If the Microsoft store hasn't been registered (test installs,
opted-out builds) the mixin returns a structured non-success
response rather than raising — the frontend treats
``unavailable`` as "show the modal".
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

class EdgeRPCMixin:
    """Edge prereq RPC : install + check."""

    registry: Any

    async def install_edge(self) -> Any:
        """Install the Edge flatpak via Microsoft store's helper.

        Wraps ``MicrosoftStore.install_edge``. The flatpak
        install can take 30-90 s on a fresh prefix ; the
        frontend modal shows a spinner during the call.

        Returns:
            ``{installed: bool, error: str | None}`` as the
            data payload. Returning a dict that does **not**
            start with a ``success`` key keeps ``_to_envelope``
            from treating this as a caller-supplied envelope
            (which would collapse ``data`` to ``None`` and
            break frontend unwrapping). ``error`` is populated
            on failure (``"edge_browser_not_configured"``,
            ``"flatpak_not_found"``, etc).
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
        logger.info("[EdgeRPC] install_edge: starting flatpak install")
        result = await store.install_edge()
        success = bool(getattr(result, "success", False))
        error = getattr(result, "error", None)
        logger.info(
            "[EdgeRPC] install_edge result: success=%s error=%s",
            success, error,
        )
        return {"installed": success, "error": error}
