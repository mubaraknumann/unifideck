"""Async file I/O helpers + safe-file-op decorator.

Two complementary surfaces:

* ``async_file_ops`` — namespace module with async wrappers
  around standard file operations (read, write, rename,
  stat, listdir, …) that run on ``asyncio.to_thread`` to
  keep the event loop responsive.
* ``safe_file_op``   — decorator factory that wraps a
  function (sync or async) with ``OSError`` handling: on
  failure, logs the offending path + exception class and
  returns a caller-supplied default value. Used to keep
  call sites linear when "file not there" or "read error"
  should be silently degraded.
"""

from . import async_file_ops
from .safe_file_op import safe_file_op

__all__ = [
    "async_file_ops",
    "safe_file_op",
]
