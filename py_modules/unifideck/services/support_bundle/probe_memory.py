"""support_bundle/probe_memory.py — capture-time heap analysis.

The companion to ``services.memory_sampler``. That service answers "is it
growing"; this module answers "growing with what", and it runs once, when
the user presses Capture Logs, because both answers here cost real work.

Two questions, deliberately separate:

* :func:`gc_type_histogram` walks the GC's object graph and counts live
  objects per type. If the backend is holding 21 GB of live objects, the
  type at the top of this list names the leak. If the process is fat but
  this histogram is unremarkable, the memory is not live Python objects
  at all — it is allocator fragmentation or a native allocation — and
  that is exactly the distinction that has been missing from every report
  so far.
* :func:`tracemalloc_top` names the allocation *sites*, which the
  histogram cannot. It only works if tracing was started at boot, so it
  is behind a config flag: ``tracemalloc`` roughly doubles allocation
  cost and must never be on by default.

Both return counts and type/module names only. No instance contents, no
titles, ids or paths from user data, which is what keeps the bundle safe
to paste in public. That rules out ``gc.get_objects()`` dumps and
``repr()`` of anything found on the heap.
"""
from __future__ import annotations

import gc
import logging
import sys
import tracemalloc
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TOP_N = 25


def gc_type_histogram(top_n: int = DEFAULT_TOP_N) -> dict[str, Any]:
    """Count live GC-tracked objects per type, biggest first.

    ``total_objects`` is the whole tracked population; ``types`` is the
    ``top_n`` slice as ``[type_name, count]`` pairs. Only the type's
    qualified name is recorded, never an instance.

    Note this sees GC-*tracked* objects only. A large ``bytes``/``str``
    payload is not tracked and will not appear here, so a fat process
    with a boring histogram is a genuine signal, not a failed probe —
    compare it against ``plugin_memory`` and the sampler series.
    """
    counts: dict[str, int] = {}
    try:
        objects = gc.get_objects()
    except Exception:
        logger.debug("[probe_memory] gc.get_objects failed", exc_info=True)
        return {}
    for obj in objects:
        cls = type(obj)
        name = f"{cls.__module__}.{cls.__qualname__}"
        counts[name] = counts.get(name, 0) + 1
    # Drop the local list before building the result so the histogram
    # itself doesn't show up as the largest thing on the heap.
    total = len(objects)
    del objects
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "total_objects": total,
        "gc_generation_counts": list(gc.get_count()),
        "garbage": len(gc.garbage),
        "types": [[name, count] for name, count in ranked[:top_n]],
    }


def tracemalloc_top(top_n: int = DEFAULT_TOP_N) -> dict[str, Any]:
    """Return the top allocation sites, if tracing is running.

    ``{"tracing": False}`` when the flag was off at boot — the normal
    case, and not an error. Frames are reported as ``file:line`` from
    the plugin's own source tree, which is code location, not user data.
    """
    if not tracemalloc.is_tracing():
        return {"tracing": False}
    try:
        snapshot = tracemalloc.take_snapshot()
        stats = snapshot.statistics("lineno")
    except Exception:
        logger.debug("[probe_memory] tracemalloc snapshot failed", exc_info=True)
        return {"tracing": True, "error": "snapshot_failed"}
    current, peak = tracemalloc.get_traced_memory()
    return {
        "tracing": True,
        "traced_current_bytes": current,
        "traced_peak_bytes": peak,
        "top": [
            {
                "location": str(stat.traceback),
                "size_bytes": stat.size,
                "count": stat.count,
            }
            for stat in stats[:top_n]
        ],
    }


def interpreter_block() -> dict[str, Any]:
    """Allocator-level facts that neither probe above covers.

    ``allocated_blocks`` is pymalloc's live block count: if it is flat
    while the process keeps growing, the growth is not in Python objects.
    """
    block: dict[str, Any] = {"gc_enabled": gc.isenabled()}
    try:
        block["allocated_blocks"] = sys.getallocatedblocks()
    except Exception:
        logger.debug("[probe_memory] getallocatedblocks failed", exc_info=True)
    return block
