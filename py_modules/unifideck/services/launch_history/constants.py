"""Launch history constants.

Small set of constants used across the launch_history sub-package
(file names, max retention size, default thresholds).
"""

from __future__ import annotations

FAILURE_KIND_FAST_BOOT = "fast_boot"
FAILURE_KIND_LAUNCHER_ERROR = "launcher_error"
_VALID_KINDS = frozenset(
    {
        FAILURE_KIND_FAST_BOOT,
        FAILURE_KIND_LAUNCHER_ERROR,
    }
)
