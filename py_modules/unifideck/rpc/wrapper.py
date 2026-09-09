"""RPC wrapper decorator — uniform envelope + structured error mapping.

``rpc_wrapper`` is the decorator wrapped around every RPC method
exposed to the frontend. It enforces a uniform response envelope
and converts exceptions into typed error payloads.

The response envelope is:

    {"success": bool, "error": str | None, "data": any | None}

Inputs the wrapper accepts from a handler:

* a plain return value → wrapped as
  ``{"success": True, "error": None, "data": <value>}``;
* an explicit dict with a ``success`` key → preserved as-is,
  with any non-``success``/``error`` keys folded into ``data``
  (or moved into a nested ``data`` if already present);
* an ``RpcError`` exception → returned as
  ``{"success": False, "error": <code>, "data": <context>}``;
* any other ``Exception`` → logged at EXCEPTION and returned
  as ``{"success": False, "error": "internal_error",
  "data": {"detail": repr(exc)}}`` (no traceback leak to the
  frontend).

``_serialize`` handles the common Python-to-JSON edge case of
dataclasses: dataclass instances are auto-converted to dicts so
handlers can return their typed result objects directly without
manual ``asdict`` calls.

Idempotent: re-wrapping an already-wrapped function returns it
unchanged (via the ``__rpc_wrapped__`` marker).
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from typing import Any, TypeVar, cast

from .errors import RpcError

logger = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

def _serialize(value: Any) -> Any:
    """Recursively convert dataclasses to dicts for JSON output.

    Walks lists, tuples and dicts so a handler returning a
    nested structure (e.g. a list of dataclass instances) is
    serialised end-to-end without manual conversion. Tuples
    are converted to lists since JSON has no tuple type.

    Bare values (str, int, None, …) pass through untouched.
    Type-objects of dataclasses (the class itself, not an
    instance) are also passed through.

    Args:
        value: arbitrary handler return data.

    Returns:
        JSON-friendly equivalent of ``value``.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, tuple):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value

def _to_envelope(value: Any) -> dict[str, Any]:
    """Coerce a handler return into the canonical RPC response envelope.

    Two cases:

    1. **Caller-supplied envelope** (dict with ``success`` key):
       respected as-is. ``error`` defaults to ``None``. The
       ``data`` field is either the existing one, or a dict
       built from the remaining keys (or ``None`` if nothing
       else was provided).
    2. **Plain return value**: wrapped as
       ``{"success": True, "error": None, "data": value}``.

    Either way, the final ``data`` goes through ``_serialize``
    so dataclasses are flattened.

    Args:
        value: the handler's return value.

    Returns:
        Three-key envelope dict ready for RPC serialisation.
    """
    if isinstance(value, dict) and "success" in value:
        success = bool(value["success"])
        error = value.get("error")
        if "data" in value:
            data = value["data"]
        else:
            data = {k: v for k, v in value.items() if k not in ("success", "error")}
            if not data:
                data = None
        return {
            "success": success,
            "error": error,
            "data": _serialize(data),
        }
    return {"success": True, "error": None, "data": _serialize(value)}

def rpc_wrapper(func: F) -> F:
    """Decorator: wrap an async RPC method with the envelope + error mapper.

    Pre-conditions:

    * ``func`` must be a coroutine function (``async def``).
      Sync functions raise ``TypeError`` immediately — async
      is required so the bus and other long-running ops can
      be awaited inside handlers.

    Behaviour:

    * Already-wrapped functions are returned as-is (idempotent
      via the ``__rpc_wrapped__`` marker), so applying the
      decorator twice doesn't double-wrap.
    * The wrapper has ``__rpc_wrapped__ = True`` after first
      wrap so ``auto_wire`` can detect it.

    Args:
        func: the coroutine function to wrap.

    Returns:
        The wrapped function, indistinguishable from ``func``
        at the type-checker level (declared ``F`` in / ``F``
        out).

    Raises:
        TypeError: when ``func`` is synchronous.
    """
    if getattr(func, "__rpc_wrapped__", False):
        return func
    if not inspect.iscoroutinefunction(func):
        raise TypeError(
            f"@rpc_wrapper requires an async function, "
            f"{func.__qualname__} is not a coroutine"
        )

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        """Run ``func``, catch errors, return the envelope.

        Three-arm dispatch:

        * **Success path** — ``_to_envelope`` builds the
          ``success=True`` response.
        * **Typed RpcError** — ``error`` field gets the code,
          ``data`` gets the context dict (or ``None`` when
          empty).
        * **Unexpected exception** — logged with full
          traceback at EXCEPTION level so plugin logs have
          the trace; frontend gets a generic ``internal_error``
          envelope with ``repr(exc)`` as the only detail (no
          leakage of internal stack frames).

        Returns:
            Three-key envelope dict.
        """
        try:
            result = await func(*args, **kwargs)
            return _to_envelope(result)
        except RpcError as err:
            return {
                "success": False,
                "error": err.code,
                "data": err.context or None,
            }
        except Exception as err:
            logger.exception(
                "[rpc] uncaught exception in %s",
                func.__qualname__,
            )
            return {
                "success": False,
                "error": "internal_error",
                "data": {"detail": repr(err)},
            }

    wrapper.__rpc_wrapped__ = True  # type: ignore[attr-defined]  # marker added by the rpc decorator
    # cast: functools.wraps infers ``_Wrapped[...]`` rather than F;
    # the runtime contract is in-F-out-F so we restore the type.
    return cast("F", wrapper)
