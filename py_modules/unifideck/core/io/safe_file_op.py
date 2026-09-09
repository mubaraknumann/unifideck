"""``@safe_file_op`` decorator — uniform OSError handling for file ops.

Most file operations across the plugin (config reads, cache
loads, manifest writes) have the same fallback contract:
``OSError`` → log + return a default. Without this decorator
every call site has to wrap with try/except, which makes the
code unreadable.

Usage::

    @safe_file_op(default=[])
    def list_games(path: str) -> list[str]: ...

The decorator detects whether the wrapped function is sync
or async and applies the right wrapper. The first positional
arg is captured as ``path_hint`` for the log line — relies on
the convention that file ops take the path first.

Refactor history (2026-05-14): ``safe_file_op`` was at CC=18
because the two inner wrappers (``async_wrapper`` and
``sync_wrapper``) duplicated the same try/except/log/return
body inside two nested closures — cognitive complexity counts
each closure body against the parent, so the duplicated logic
hit twice. Pulled the failure-logging into a top-level helper
(``_log_file_op_failure``) so each wrapper is now a 4-line
try/except/log/return ; complexity is back to single digits.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")
_Callable = Callable[..., T | Awaitable[T]]

def _log_file_op_failure(
    fname: str,
    log_level: int,
    exc: OSError,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    """Emit the uniform ``[safe_file_op] <fn>(<path>) failed`` log line.

    Extracted out of the two wrappers so the wrappers stay
    short and the log format stays exactly one place to update.

    The path-hint extraction (``args[0] or kwargs["path"]``)
    reflects the project convention: file ops always take their
    path as first positional or as a ``path`` kwarg. Doesn't
    reach further into kwargs to keep the logic predictable.
    """
    path_hint = args[0] if args else kwargs.get("path", "?")
    logger.log(
        log_level,
        "[safe_file_op] %s(%r) failed: %s: %s",
        fname,
        path_hint,
        type(exc).__name__,
        exc,
    )

def safe_file_op(
    default: Any = None,
    *,
    log_level: int = logging.WARNING,
) -> Callable[[_Callable[T]], Callable[..., Any]]:
    """Decorator factory that wraps a file op with ``OSError`` handling.

    Returns the actual decorator (the factory pattern lets
    callers configure the default value + log level per call
    site). The decorator inspects ``fn``: if it's a
    coroutine function, builds an async wrapper; otherwise
    builds a sync wrapper. Both wrappers have identical
    semantics — try, catch ``OSError``, log, return default.

    Only ``OSError`` is caught — other exceptions (logic
    errors, unexpected types) propagate normally. This is
    deliberate: ``OSError`` covers the legitimate "filesystem
    said no" cases (file missing, permission denied, disk
    full, broken symlink) without swallowing programmer
    bugs.

    Args:
        default: value to return on ``OSError``. Defaults
            to ``None``; callers typically pass ``[]`` or
            ``{}`` for collection-returning ops.
        log_level: ``logging`` level for the failure log
            line. Defaults to WARNING; pass DEBUG for ops
            that fail noisily and harmlessly (e.g. cache
            misses).

    Returns:
        The actual decorator, ready to apply to a function.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Pick async or sync wrapper based on ``fn``'s nature.

        ``asyncio.iscoroutinefunction`` is checked at
        decoration time (not call time) so wrapper
        selection is one-shot per decorated function — no
        per-call overhead.
        """
        fname = getattr(fn, "__name__", repr(fn))
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                """Await ``fn``; on OSError log + return default."""
                try:
                    return await fn(*args, **kwargs)
                except OSError as exc:
                    _log_file_op_failure(fname, log_level, exc, args, kwargs)
                    return default

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            """Call ``fn`` sync; on OSError log + return default."""
            try:
                return fn(*args, **kwargs)
            except OSError as exc:
                _log_file_op_failure(fname, log_level, exc, args, kwargs)
                return default

        return sync_wrapper

    return decorator
