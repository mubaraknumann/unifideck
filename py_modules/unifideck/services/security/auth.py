"""services.security.mixins.auth — OAuth flow observability handlers.

Four @subscribe handlers that observe OAuth authentication
flows across all 7 stores:

  - SECURITY_AUTH_FLOW_STARTED
  - SECURITY_AUTH_FLOW_COMPLETED
  - SECURITY_AUTH_FLOW_FAILED
  - SECURITY_EXTERNAL_AUTH_CHECK_FAILED  (for Epic/Amazon/Ubisoft
                                          which are outside
                                          Unifideck's cryptographic
                                          protection scope but
                                          whose status probes
                                          still surface anomalies)

Mixed into ``SecurityService`` via multiple inheritance so the
@subscribe decorators are picked up by ``auto_wire``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.types.events import Events
from unifideck.event_bus.event_bus_devex import subscribe

if TYPE_CHECKING:
    from .audit_log import AuditLog

logger = logging.getLogger(__name__)


class AuthAuditMixin:
    """Record + react to OAuth flow lifecycle events.

    Expects the host class to provide:

      - ``self._audit`` : ``AuditLog`` instance
    """

    _audit: AuditLog

    @subscribe(Events.SECURITY_AUTH_FLOW_STARTED)
    async def _on_auth_started(self, **kwargs: Any) -> None:
        """Record the start of an OAuth flow on any store."""
        self._audit.record("SECURITY_AUTH_FLOW_STARTED", kwargs)

    @subscribe(Events.SECURITY_AUTH_FLOW_COMPLETED)
    async def _on_auth_completed(self, **kwargs: Any) -> None:
        """Record a successful OAuth flow completion."""
        self._audit.record("SECURITY_AUTH_FLOW_COMPLETED", kwargs)

    @subscribe(Events.SECURITY_AUTH_FLOW_FAILED)
    async def _on_auth_failed(self, **kwargs: Any) -> None:
        """Record an OAuth flow failure with its reason."""
        self._audit.record("SECURITY_AUTH_FLOW_FAILED", kwargs)
        reason = kwargs.get("reason", "unknown")
        logger.warning(
            "[SecurityService] auth flow failed: %s", reason,
        )

    @subscribe(Events.SECURITY_EXTERNAL_AUTH_CHECK_FAILED)
    async def _on_external_auth_check_failed(
        self, **kwargs: Any,
    ) -> None:
        """Record an anomaly in an external credential reader.

        Emitted by Epic/Amazon/Ubisoft stores when their status
        probes hit a genuine failure (missing CLI, corrupt file,
        broken prefix). Not emitted for routine "user not logged
        in" cases. Logs at warning level so the anomaly is
        visible in the plugin log even if the DiagnosticsPanel
        isn't open.
        """
        self._audit.record(
            "SECURITY_EXTERNAL_AUTH_CHECK_FAILED", kwargs,
        )
        store = kwargs.get("store", "unknown")
        reason = kwargs.get("reason", "unknown")
        logger.warning(
            "[SecurityService] external auth check failed: "
            "%s / %s", store, reason,
        )
