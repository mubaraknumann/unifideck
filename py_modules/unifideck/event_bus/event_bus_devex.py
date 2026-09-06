"""Bus developer-experience helpers — auto-wire + introspection.

Developer-facing helpers that make working with the bus less
boilerplate-heavy:

* ``_Subscription``         — typed record of one subscription
  declaration (event, handler, priority, timeout, scope).
* ``SubscriptionRegistry``  — in-process registry where the
  ``@subscribe`` decorator stashes its declarations for later
  application to a bus.
* ``subscribe(event, ...)`` — decorator. On a free function it
  registers immediately; on an instance method it just stamps
  metadata for ``auto_wire`` to pick up later.
* ``auto_wire(obj, bus)``   — walk an object's attributes for
  ``@subscribe``-decorated methods and register them on the
  bus, with optional watchdog registration and registry
  recording.
* ``SchemaExtractor``       — AST-based static analysis to
  extract per-event kwarg sets from emit/enqueue call sites
  in source code (used by tooling to bootstrap
  ``EventSchema`` declarations).

This module exists to keep the core ``EventBus`` API minimal —
the decorator + auto-wire pattern lives here, not on the bus
itself.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    ParamSpec,
    TypeVar,
)

if TYPE_CHECKING:
    from unifideck.core.types import Events

    from .event_bus import EventBus
    from .supervision.watchdog_handler import HandlerWatchdog

logger = logging.getLogger(__name__)

# Type variables used by the ``subscribe`` decorator to preserve
# the decorated handler's signature through the decoration pass.
# Without these, mypy --strict reports
# ``Untyped decorator makes function "X" untyped`` on every
# ``@subscribe(...)`` site (~30 occurrences).
#
# ``_P`` captures the handler's parameter list (positional + kw)
# and ``_R`` its return type — either an awaitable result for
# async handlers or whatever the sync handler returns. The
# decorator returns ``Callable[_P, _R]`` so the original
# signature flows through unchanged at call sites.
_P = ParamSpec("_P")
_R = TypeVar("_R")

@dataclass
class _Subscription:
    """One subscription declaration produced by ``@subscribe``.

    Attributes:
        event: event identifier (always stringified; the enum
            members are converted to their ``.value``).
        handler: the callable target.
        priority: optional priority hint for dispatchers that
            care (the basic ``EventBus`` ignores it; the
            ``PriorityDispatcher`` uses it).
        timeout: optional per-handler watchdog timeout override.
        scope: optional free-form tag for diagnostics
            (e.g. ``"security"``).
    """

    event: str
    handler: Callable[..., Any]
    priority: int | None = None
    timeout: float | None = None
    scope: str | None = None

class SubscriptionRegistry:
    """Append-only registry of declared subscriptions.

    Populated by the ``@subscribe`` decorator for free-function
    handlers (instance-method handlers are registered later by
    ``auto_wire``). ``apply(bus)`` walks the registry and
    actually subscribes everything on the given bus.
    """

    def __init__(self) -> None:
        """Initialise an empty subscription list.

        No global setup; each registry instance is independent.
        The ``default_registry`` module-level instance is used
        by the bare ``@subscribe`` decorator when no explicit
        registry is passed.
        """
        self._subs: list[_Subscription] = []

    def add(self, sub: _Subscription) -> None:
        """Append a subscription record to the registry.

        Called by the ``@subscribe`` decorator (for free
        functions) and by ``auto_wire`` (for instance methods
        when a registry is supplied).

        Args:
            sub: the subscription declaration to record.
        """
        self._subs.append(sub)

    def all(self) -> list[_Subscription]:
        """Return a shallow copy of every recorded subscription.

        Order is insertion order. Shallow copy so the caller
        can iterate while new subscriptions are added on
        another task (rare but possible).

        Returns:
            List of ``_Subscription`` records.
        """
        return list(self._subs)

    def apply(self, bus: EventBus) -> int:
        """Subscribe every recorded handler on the given bus.

        Iterates the registry and calls ``bus.on(event, handler)``
        for each. Defensive ``hasattr`` check on ``bus.on``
        means a half-baked stub bus (used in some tests) won't
        crash here.

        Args:
            bus: the target ``EventBus``.

        Returns:
            Number of subscriptions actually applied (matches
            the registry size unless ``bus`` lacked an ``on``
            method, in which case 0).
        """
        count = 0
        for s in self._subs:
            if hasattr(bus, "on"):
                bus.on(s.event, s.handler)
                count += 1
        return count

    def clear(self) -> None:
        """Drop every recorded subscription.

        Test-only — production code that wants to rebuild the
        subscription graph should typically build a fresh
        registry instance instead.
        """
        self._subs.clear()

default_registry = SubscriptionRegistry()

def _build_subscription(
    fn: Callable[_P, _R],
    event: str | Events,
    *,
    priority: int | None,
    timeout: float | None,
    scope: str | None,
) -> _Subscription:
    """Construct a ``_Subscription`` record from a handler.

    Extracted from the ``subscribe`` decorator so the
    decorator stays under the function-length cap. The
    ``event`` argument is coerced to its string form
    (enum members are unwrapped via ``.value``).
    """
    event_key = getattr(event, "value", event)
    return _Subscription(
        event=str(event_key),
        # ``handler`` on ``_Subscription`` is typed as
        # ``Callable[..., Any]``; we keep ``fn``'s precise
        # signature for the caller but the registry only
        # needs an invokable, so silence the variance here.
        handler=fn,
        priority=priority,
        timeout=timeout,
        scope=scope,
    )

def subscribe(
    event: str | Events,
    *,
    priority: int | None = None,
    timeout: float | None = None,
    scope: str | None = None,
    registry: SubscriptionRegistry | None = None,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Decorator factory that marks a function as a bus subscriber.

    Two registration paths:

    * **Free function** — appended directly to the registry
      (``default_registry`` if no explicit one was given).
    * **Instance method** — detected via
      ``_looks_like_instance_method``; the decorator only
      stamps ``__subscribe_meta__`` and defers registration
      to ``auto_wire(self, bus)`` once an instance exists.

    The return type ``Callable[[Callable[_P, _R]], Callable[_P, _R]]``
    preserves the decorated function's signature through the
    pass so mypy --strict doesn't downgrade callers to ``Any``.

    Args:
        event: event identifier (``Events`` or string).
        priority: optional dispatcher priority hint.
        timeout: optional watchdog timeout override.
        scope: optional diagnostic tag.
        registry: optional registry; defaults to ``default_registry``.
    """

    def decorator(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        """Stamp ``fn`` with subscription metadata and register if free."""
        meta = _build_subscription(
            fn,
            event,
            priority=priority,
            timeout=timeout,
            scope=scope,
        )
        # ``__subscribe_meta__`` is a runtime attribute used by
        # ``auto_wire`` to discover decorated methods. Callable
        # types don't formally allow attribute assignment.
        fn.__subscribe_meta__ = meta  # type: ignore[attr-defined]
        if _looks_like_instance_method(fn):
            return fn
        reg = registry or default_registry
        reg.add(meta)
        return fn

    return decorator

def _looks_like_instance_method(fn: Callable[..., Any]) -> bool:
    """Heuristic: does ``fn``'s first parameter look like ``self`` / ``cls``?

    Inspects the function signature and checks the first
    parameter's name. Decorated methods being inspected before
    binding don't yet have ``__self__``, so we rely on the
    name convention.

    Returns ``False`` on inspection errors (some builtins or
    C-level callables don't have an inspectable signature) so
    those get registered immediately as free functions — the
    safer default.

    Args:
        fn: the callable to inspect.

    Returns:
        ``True`` iff the first parameter is named ``self`` or
        ``cls``.
    """
    try:
        import inspect

        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        return bool(params) and params[0] in ("self", "cls")
    except (TypeError, ValueError):
        return False

def auto_wire(
    instance: Any,
    bus: EventBus,
    *,
    registry: SubscriptionRegistry | None = None,
    watchdog: HandlerWatchdog | None = None,
) -> int:
    """Walk ``instance``'s attributes and subscribe ``@subscribe``-marked methods.

    For each public attribute (no leading ``__``):

    1. Resolve the attribute and any ``__subscribe_meta__``
       stamp via ``_resolve_subscribe_target`` (which also
       handles bound methods, where the metadata lives on
       ``__func__``).
    2. If a metadata stamp exists, call ``bus.on(event, method)``
       — registering the **bound** method so the bus calls
       ``instance.method(...)`` correctly.
    3. Optionally register the handler name with the watchdog
       and append a record to the registry.

    Used by every service's ``__init__`` to wire its bus
    handlers without explicit ``bus.on(...)`` calls — keeps
    constructor bodies declarative.

    Args:
        instance: the object whose methods will be inspected.
        bus: target bus.
        registry: optional registry to record into.
        watchdog: optional watchdog to register handler names
            on.

    Returns:
        Number of methods that were actually subscribed.
    """
    # Fall back to the bus's own watchdog when the caller did not pass one.
    #
    # This parameter existed from the start and **every one of the ~20
    # ``auto_wire`` call sites passes two positional arguments**, so
    # ``watchdog`` was always ``None``, ``_register_with_watchdog`` never
    # ran, and ``HandlerWatchdog`` tracked zero handlers for the life of the
    # project — a support bundle reported ``watchdog: {}`` against 42
    # registered events, which reads as "healthy" rather than "not wired"
    # (audit register item 4g).
    #
    # Reading it off the bus rather than editing 20 constructors is what
    # makes this safe to land on its own: services are constructed with a
    # bus and have no access to the pipeline, so threading it by hand would
    # have meant a new parameter on every service. ``pipeline_factory``
    # attaches it once.
    if watchdog is None:
        watchdog = getattr(bus, "watchdog", None)

    count = 0
    for attr_name in dir(instance):
        if attr_name.startswith("__"):
            continue
        attr, meta = _resolve_subscribe_target(instance, attr_name)
        if meta is None or attr is None:
            # ``meta is None`` covers the "not a subscriber" case;
            # ``attr is None`` is paranoid (shouldn't happen when
            # meta is present) but the explicit guard narrows the
            # type from ``Any | None`` to ``Any`` for mypy strict.
            continue
        if not hasattr(bus, "on"):
            continue
        bus.on(meta.event, attr)
        count += 1
        if watchdog is not None:
            _register_with_watchdog(instance, attr_name, watchdog, meta.timeout)
        if registry is not None:
            registry.add(
                _Subscription(
                    event=meta.event,
                    handler=attr,
                    priority=meta.priority,
                    timeout=meta.timeout,
                    scope=meta.scope,
                ),
            )
    return count

def _resolve_subscribe_target(
    instance: Any, attr_name: str,
) -> tuple[Any | None, _Subscription | None]:
    """Fetch an attribute and its ``__subscribe_meta__`` stamp.

    Two-layer lookup:

    1. ``getattr(instance, attr_name)`` to get the attribute
       (may be a bound method).
    2. ``__subscribe_meta__`` on the attribute first, falling
       back to ``__subscribe_meta__`` on the underlying
       ``__func__`` for bound methods (the decorator stamps
       the function, not the bound method).

    Returns ``(None, None)`` when the attribute is missing or
    has no metadata so the caller can simply skip without
    further checks.

    Args:
        instance: object being scanned.
        attr_name: name of the attribute to resolve.

    Returns:
        Tuple ``(attribute, metadata)`` — both ``None`` when no
        match, otherwise the bound callable and its
        ``_Subscription`` stamp.
    """
    try:
        attr = getattr(instance, attr_name)
    except AttributeError:
        return None, None
    meta = getattr(attr, "__subscribe_meta__", None)
    if meta is None:
        func = getattr(attr, "__func__", None)
        if func is not None:
            meta = getattr(func, "__subscribe_meta__", None)
    return (attr if meta is not None else None), meta

def _register_with_watchdog(
    instance: Any,
    attr_name: str,
    watchdog: HandlerWatchdog,
    timeout: float | None = None,
) -> None:
    """Register the qualified handler name with the watchdog.

    Builds the canonical name as ``<ClassName>.<attr_name>``
    so the watchdog's metrics are readable
    (``ShortcutService._on_download_complete`` rather than
    just ``_on_download_complete``).

    ``timeout`` is the handler's ``@subscribe(timeout=...)``
    override. It used not to be forwarded: this function called
    ``watchdog.register(qualname)`` with one argument, so
    ``HandlerWatchdog`` fell back to its 5s
    ``DEFAULT_HANDLER_TIMEOUT_SEC`` for *every* handler and a
    declared override was silently inert — the meta reached only the
    introspection-only ``SubscriptionRegistry``, which enforces
    nothing. That is what made ``ShortcutService._on_sync_complete``
    undeclarable: a full 1242-game reconcile legitimately takes ~5s,
    so it was being cancelled mid-write by a budget it had no way to
    raise, leaving shortcuts.vdf unwritten and
    ``SHORTCUT_RECONCILE_COMPLETE`` never emitted.

    Watchdog registration failures (e.g. a stub watchdog
    without a ``register`` method) are caught and logged at
    DEBUG — the bus subscription still succeeds, the handler
    just won't have watchdog supervision.

    Args:
        instance: the host instance.
        attr_name: the method's attribute name.
        watchdog: the watchdog to register on.
        timeout: per-handler budget in seconds, or ``None`` for the
            watchdog's default.
    """
    qualname = f"{type(instance).__name__}.{attr_name}"
    try:
        watchdog.register(qualname, timeout=timeout)
    except (AttributeError, RuntimeError) as e:
        logger.debug(
            "[event_bus_devex] watchdog register failed for %s: %s",
            qualname,
            e,
        )

class SchemaExtractor:
    """Static analysis: pull per-event kwarg sets out of source code.

    Walks the AST of a source string, finds every
    ``bus.emit(EVENT, k1=..., k2=...)`` or
    ``dispatcher.enqueue(EVENT, k1=..., k2=...)`` call, and
    aggregates the kwarg names per event.

    Used by build-time tooling to bootstrap ``EventSchema``
    declarations: scan every emit site in the codebase, take
    the union of kwargs seen for each event, and you have the
    runtime contract for free.

    Not used at runtime — pure static analysis helper.
    """

    @staticmethod
    def extract_from_source(source: str) -> dict[str, set[str]]:
        """Parse ``source`` and return ``event_name → {kwarg_name, ...}``.

        AST parsing failures (syntax errors in the source)
        are caught and surface as an empty dict — the caller
        treats malformed files as "nothing extractable" rather
        than aborting.

        Args:
            source: Python source code as a string.

        Returns:
            Mapping ``event_name → set_of_kwarg_names`` for
            every detected emit/enqueue site.
        """
        out: dict[str, set[str]] = {}
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return out
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not SchemaExtractor._is_emit_call(node):
                continue
            event_name = SchemaExtractor._extract_event_name(node)
            if event_name is None:
                continue
            kwarg_names = {kw.arg for kw in node.keywords if kw.arg is not None}
            out.setdefault(event_name, set()).update(kwarg_names)
        return out

    @staticmethod
    def _is_emit_call(node: ast.Call) -> bool:
        """Return whether the AST call is a ``.emit(...)`` or ``.enqueue(...)``.

        Method-call detection only — bare ``emit(...)``
        function calls aren't matched (they don't exist in
        the codebase anyway). The accepted names are
        deliberately narrow: ``emit`` for the basic bus,
        ``enqueue`` for the priority dispatcher.

        Args:
            node: the AST call node.

        Returns:
            ``True`` for matching method calls, ``False``
            otherwise.
        """
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "emit":
            return True
        return bool(isinstance(func, ast.Attribute) and func.attr == "enqueue")

    @staticmethod
    def _extract_event_name(node: ast.Call) -> str | None:
        """Pull the event name out of the first positional argument.

        Two accepted shapes:

        * ``Events.SOMETHING`` — read ``Attribute.attr`` (the
          enum member name).
        * ``"event.name"``     — read ``Constant.value`` (the
          string literal).

        Anything else (a variable, a function call) returns
        ``None`` — the static analysis can't follow data flow.

        Args:
            node: the AST call node.

        Returns:
            The event name as a string, or ``None`` if the
            first argument doesn't match the expected shapes.
        """
        if not node.args:
            return None
        first = node.args[0]
        if isinstance(first, ast.Attribute):
            return first.attr
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return None
