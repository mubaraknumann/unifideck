"""Symmetric shutdown — counterpart to ``bootstrap_services``.

``stop_all_services(container)`` walks the service container in
reverse construction order and calls each service's ``stop()``
coroutine (or skips silently if absent). Used by ``Plugin._unload`` to
release file handles, close DB connections, drain in-flight tasks
with a deadline before Decky kills the process.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .container import ServiceContainer
logger = logging.getLogger(__name__)

async def stop_all_services(container: ServiceContainer) -> None:
    """Tear down every service in reverse-dependency order.

    Iterates a hard-coded teardown order that mirrors the
    construction order in reverse — services that depend on others
    are stopped before their dependencies. The list is explicit
    (rather than derived from ``_SERVICE_DEFS``) so it's reviewable
    at a glance and a developer changing service wiring is forced
    to think about teardown.

    For each service, prefers ``stop()`` and falls back to
    ``disconnect()`` (used by the CDP client, which has a
    network-shutdown semantic rather than a generic stop).

    Per-service failures are tolerated (logged at WARN) so one
    broken teardown doesn't leave subsequent services hanging — at
    plugin unload Decky will kill the process anyway after a short
    deadline.

    Args:
        container: the populated ``ServiceContainer`` to drain.
    """
    teardown_order = [
        "cloud_prompt",
        "security",
        "probe_reaction",
        "feature_flags",
        "achievements",
        "playtime_sync",
        "playtime",
        "account",
        "metrics",
        "cloudsave",
        "proton",
        "artwork",
        "metadata",
        "download",
        "shortcut",
        "cdp",
    ]
    for attr in teardown_order:
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
