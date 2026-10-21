"""XboxPcRPCMixin — Xbox PC games through our own Xodus build.

A thin proxy to the ``XboxPc`` facade the Microsoft store owns
(``stores/microsoft/xodus/xbox_pc.py``). The status routes answer with the
status snapshot (``XboxPcStatus`` in ``src/types/api.ts``); the component
install answers ``{started, reason}``, its cancel ``{cancelled}`` and the
sign-in start ``{pending, launch_shortcut, reason, status}``. No payload has a
top-level ``success`` key, so the envelope wraps it as data instead of
lifting it.

A refused request raises ``RpcError`` with the facade's code (for example
``notice_not_acknowledged``), which the frontend's throwing ``useRPC``
surfaces. With no Microsoft store registered, the status routes answer
``available: false`` and the QAM hides the section.
"""

from __future__ import annotations

import logging
from typing import Any

from unifideck.rpc.errors import RpcError
from unifideck.stores.microsoft.xodus.xbox_pc import XboxPcError, unavailable_status

logger = logging.getLogger(__name__)


class XboxPcRPCMixin:
    """Xbox PC toggle, components, sign-in and status."""

    registry: Any

    def _xbox_pc(self) -> Any:
        """The facade, or None when the Microsoft store is missing."""
        store = self.registry.get("microsoft")
        return getattr(store, "xbox_pc", None) if store is not None else None

    def _require_xbox_pc(self) -> Any:
        facade = self._xbox_pc()
        if facade is None:
            raise RpcError("xbox_pc_unavailable")
        return facade

    async def get_xbox_pc_status(self) -> dict[str, Any]:
        """The cached status."""
        facade = self._xbox_pc()
        return await facade.status() if facade is not None else unavailable_status()

    async def refresh_xbox_pc_status(self) -> dict[str, Any]:
        """Probe the components and ask Xodus about the account again."""
        facade = self._xbox_pc()
        return await facade.refresh_status() if facade is not None else unavailable_status()

    async def set_xbox_pc_enabled(
        self, enabled: bool, notice_version: int | None = None,
    ) -> dict[str, Any]:
        """Turn the feature on (after the notice) or off (deletes nothing)."""
        facade = self._require_xbox_pc()
        try:
            status: dict[str, Any] = await facade.set_enabled(bool(enabled), notice_version)
        except XboxPcError as e:
            raise RpcError(e.code) from None
        return status

    async def install_xbox_pc_runtime(self) -> dict[str, Any]:
        """Start installing the backend and runtime: ``{started, reason}``.

        Progress and the end arrive as ``XBOX_PC_STATE_CHANGED``.
        """
        result: dict[str, Any] = await self._require_xbox_pc().install_components()
        return result

    async def cancel_xbox_pc_runtime_install(self) -> dict[str, Any]:
        """Stop a running component install: ``{cancelled}``."""
        result: dict[str, Any] = await self._require_xbox_pc().cancel_install()
        return result

    async def start_xbox_pc_sign_in(self) -> dict[str, Any]:
        """Start the Xodus sign-in: ``{pending, launch_shortcut, reason, status}``.

        On ``pending`` the frontend launches the ``launch_shortcut`` auth
        shortcut; if that launch fails it calls ``cancel_xbox_pc_sign_in``.
        """
        result: dict[str, Any] = await self._require_xbox_pc().start_sign_in()
        if not result.get("pending"):
            logger.info("[XboxPcRPC] sign-in refused: %s", result.get("reason"))
        return result

    async def cancel_xbox_pc_sign_in(self) -> dict[str, Any]:
        """Stop a running sign-in (no failure is recorded)."""
        status: dict[str, Any] = await self._require_xbox_pc().cancel_sign_in()
        return status

    async def sign_out_xbox_pc(self) -> dict[str, Any]:
        """Forget the Xodus account. Refused with ``xodus_busy`` mid-download."""
        try:
            status: dict[str, Any] = await self._require_xbox_pc().sign_out()
        except XboxPcError as e:
            raise RpcError(e.code) from None
        return status
