"""services/bootstrap/store_injector.py — Post-discovery store DI.

Cross-service dependency injection that runs AFTER both
``auto_discover`` (instantiates stores with minimal signature)
and ``bootstrap_services`` (builds the container). Walks
``_STORE_INJECTIONS`` and ``setattr``'s each mapping onto the
live store instance.

Rationale for late injection: ``auto_discover`` is a generic
scanner that knows nothing of store-specific Layer-5 deps.
Rather than expand its signature to accept every possible kwarg,
we keep ``auto_discover`` uniform and do specialised wiring here.

Refactor history (2026-05-14): ``inject_store_dependencies`` was
a single function at CC=16 — a double ``for store / for mapping``
loop with three separate failure paths (``registry.get`` raising
``KeyError``, raising another exception, or returning ``None``),
plus another failure path on ``setattr`` (``__slots__`` /
frozen). Split into three helpers so the main loop reads as
"for each store, resolve, then wire each mapping, then rebuild
auth if applicable".
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unifideck.stores import StoreRegistry

    from .container import ServiceContainer

logger = logging.getLogger(__name__)

# Post-construction wiring table.
# Each entry: store_id → tuple of (store_attr, container_attr).
# The assignment is conditional: None container slot (service
# failed to instantiate) leaves the store attribute at its
# constructor default, disabling the feature with a WARNING.
_STORE_INJECTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "amazon": (
        ("_browser_monitor", "browser_monitor"),
        ("_shortcut_service", "shortcut"),
        ("_edge", "edge_browser"),
    ),
    "epic": (
        ("_browser_monitor", "browser_monitor"),
        ("_shortcut_service", "shortcut"),
        ("_edge", "edge_browser"),
    ),
    "gog": (
        ("_browser_monitor", "browser_monitor"),
        ("_shortcut_service", "shortcut"),
        ("_edge", "edge_browser"),
    ),
    "microsoft": (
        ("_browser_monitor", "browser_monitor"),
        ("_shortcut_service", "shortcut"),
        ("_edge", "edge_browser"),
        ("_subscription_service", "microsoft_subscription"),
    ),
    # itch.io signs in through the standard Edge window (browser monitor +
    # Edge for the post-login steering); butler owns everything after that.
    "itch": (
        ("_browser_monitor", "browser_monitor"),
        ("_edge", "edge_browser"),
    ),
    "ubisoft": (
        ("_shortcut_service", "shortcut"),
    ),
    # Battle.net signs in through the vendor client in its own prefix, so it
    # needs the shortcut service to place the auth tile. It deliberately does
    # NOT take the Edge/CDP services: ownership is read from the client's
    # local licence ledger, and the web endpoint is optional enrichment only.
    "battlenet": (
        ("_shortcut_service", "shortcut"),
    ),
}


def inject_store_dependencies(
    registry: StoreRegistry | None,
    container: ServiceContainer,
) -> None:
    """Inject Layer-5 service refs into already-registered stores.

    Called post-``auto_discover`` + post-``bootstrap_services``.
    Walks ``_STORE_INJECTIONS`` and delegates the actual work to
    three helpers so this orchestrator stays under the cognitive
    complexity gate:

    * ``_resolve_store`` — look up the store, swallow registry
      errors (returns None on miss);
    * ``_inject_one`` — set one attribute, log success / skip /
      failure cases;
    * ``_maybe_rebuild_auth`` — invoke the optional
      ``_rebuild_auth_after_injection`` hook on stores that
      need to reconstruct their auth orchestrator now that
      the browser monitor is wired.

    Failures are isolated per-store and per-attribute: any
    registry miss, missing container slot, setattr failure
    (``__slots__`` / frozen dataclass), or auth-rebuild
    exception leaves the rest of the wiring untouched.

    ``registry=None`` → silent no-op (test harness).
    """
    if registry is None:
        return

    for store_id, injections in _STORE_INJECTIONS.items():
        store_instance = _resolve_store(registry, store_id)
        if store_instance is None:
            continue
        for store_attr, container_attr in injections:
            _inject_one(
                store_instance, store_id, store_attr,
                container_attr, container,
            )
        _maybe_rebuild_auth(store_instance, store_id)


def _resolve_store(registry: StoreRegistry, store_id: str) -> Any | None:
    """Look up ``store_id`` on ``registry``, return None on any failure.

    ``StoreRegistry.get`` may raise ``KeyError`` (store not
    auto-discovered), other exceptions (broken plugin code), or
    return ``None`` (registered but no instance built). All three
    map to the same outcome: skip this store, log at INFO.
    """
    try:
        store = registry.get(store_id)
    except KeyError:
        logger.info("[bootstrap] store %s not registered — skipping injection", store_id)
        return None
    except Exception as e:
        logger.warning(
            "[bootstrap] store %s lookup raised %s — skipping injection",
            store_id, e,
        )
        return None
    if store is None:
        # Defensive: ``registry.get`` is typed as returning a
        # ``Store`` but legacy code paths could return None for a
        # registered-but-not-built entry. Keep the guard; mypy
        # flags it as unreachable but removing it would crash
        # the bootstrap on edge cases.
        logger.info("[bootstrap] store %s has no instance — skipping injection", store_id)  # type: ignore[unreachable]
        return None
    return store


def _inject_one(
    store: Any,
    store_id: str,
    store_attr: str,
    container_attr: str,
    container: ServiceContainer,
) -> None:
    """Set ``store.<store_attr> = container.<container_attr>`` with logging.

    Three outcomes:

    * Container slot is None (service failed to instantiate) →
      INFO log, store attribute left at its constructor default.
    * setattr raises (``__slots__`` declared, frozen dataclass,
      property without setter) → WARNING log, feature disabled
      for this store but the rest of the wiring continues.
    * Success → INFO log including both ends of the wire so the
      DI graph is grep-friendly in plugin logs.
    """
    svc = getattr(container, container_attr, None)
    if svc is None:
        logger.info(
            "[bootstrap] %s.%s not injected (container.%s is None)",
            store_id, store_attr, container_attr,
        )
        return
    try:
        setattr(store, store_attr, svc)
    except Exception as e:
        logger.warning(
            "[bootstrap] failed to inject %s.%s: %s",
            store_id, store_attr, e,
        )
        return
    logger.info(
        "[bootstrap] injected %s.%s ← container.%s",
        store_id, store_attr, container_attr,
    )


def _maybe_rebuild_auth(store: Any, store_id: str) -> None:
    """Trigger ``store._rebuild_auth_after_injection()`` when defined.

    Stores that hold an auth orchestrator built lazily on top of
    the browser monitor expose this hook to reconstruct the
    orchestrator now that injection has filled
    ``_browser_monitor``. Ubisoft also defines it — to propagate the
    just-injected ``_shortcut_service`` into its auth facade (which
    captured ``None`` at construction). Stores without the hook just
    skip it.

    Exceptions are swallowed with a WARNING — auth rebuild
    failure should not prevent the rest of the boot from
    finishing.
    """
    rebuild = getattr(store, "_rebuild_auth_after_injection", None)
    if not callable(rebuild):
        return
    try:
        rebuild()
    except Exception as e:
        logger.warning("[bootstrap] %s auth rebuild failed: %s", store_id, e)
        return
    logger.info("[bootstrap] %s auth rebuilt after injection", store_id)
