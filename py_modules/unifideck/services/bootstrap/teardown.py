"""Symmetric shutdown — counterpart to ``bootstrap_services``.

``stop_all_services(container)`` walks the service container in
reverse construction order and calls each service's ``stop()``
coroutine (or skips silently if absent). Used by ``Plugin._unload`` to
release file handles, close DB connections, drain in-flight tasks
with a deadline before Decky kills the process.
"""

from __future__ import annotations

import logging
from dataclasses import fields
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .container import ServiceContainer
logger = logging.getLogger(__name__)


def teardown_order(container: ServiceContainer) -> list[str]:
    """Return every container field name, in reverse construction order.

    ``ServiceContainer`` declares its fields in construction order, so
    reversing them stops a service before anything it was built on top
    of — the property the old hand-maintained list was trying to encode
    by hand.

    That list is gone because it silently drifted: it omitted
    ``compatibility`` (whose ``_enrichment_task`` was therefore never
    cancelled), ``activity_log``, ``launch_history``, ``launch_logs``,
    ``support_bundle``, ``microsoft_subscription``, ``browser_monitor``,
    ``edge_browser`` and ``user_paths_coordinator``, and it listed a
    ``cloud_prompt`` that has never been a field at all. Deriving the
    order means a service added to the container cannot be forgotten
    here, which is the drift class the architecture checks exist to
    catch.
    """
    return [f.name for f in reversed(fields(container))]


async def stop_all_services(container: ServiceContainer) -> None:
    """Tear down every service in reverse-construction order.

    For each service, prefers ``stop()`` and falls back to
    ``disconnect()`` (used by the CDP client, which has a
    network-shutdown semantic rather than a generic stop). A service
    that is unset or exposes neither is skipped.

    Per-service failures are tolerated (logged at WARN) so one
    broken teardown doesn't leave subsequent services hanging — at
    plugin unload Decky will kill the process anyway after a short
    deadline.

    Args:
        container: the populated ``ServiceContainer`` to drain.
    """
    for attr in teardown_order(container):
        svc = getattr(container, attr, None)
        if svc is None:
            continue
        stop_fn = getattr(svc, "stop", None) or getattr(svc, "disconnect", None)
        if stop_fn is None:
            continue
        try:
            await stop_fn()
        except Exception as e:
            logger.warning(
                "[bootstrap] %s.%s raised: %s",
                attr,
                stop_fn.__name__,
                e,
            )
