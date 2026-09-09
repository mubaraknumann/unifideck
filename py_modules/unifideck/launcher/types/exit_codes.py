from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Exit code."""
    SUCCESS = 0
    GENERIC_ERROR = 1
    CONFIG_INVALID = 2
    DEPENDENCY_MISSING = 3
    NETWORK_ERROR = 4
    CANCELLED_BY_USER = 5
    TIMED_OUT = 6
    PREFIX_CORRUPTED = 7
    GAME_FAILED = 8
    CIRCUIT_BREAKER_OPEN = 9
    SIGTERM_EQUIVALENT = 143
    # There is deliberately no ``user_message_key``. It mapped each exit
    # code to a ``toasts.launcher.*`` i18n key and had zero callers — and
    # **eight of the nine keys it returned were never written into any
    # locale**, so wiring it would have shown the user the raw key name.
    #
    # That is the inverse of the audit's usual finding: §1.1.2 had strings
    # translated into all 16 locales with the delivery channel dead; here the
    # delivery half existed and the strings never did. Nothing caught it
    # because ``check_orphan_keys`` check 1 only scans ``t("key")`` in
    # ``src/`` — a key named from Python was checked in neither direction.
    # Check 4 now closes that (all 48 real ``i18n_key=`` literals resolve).
    #
    # The capability is worth having: a launch that times out or hits a
    # missing dependency should say so instead of the one generic
    # ``toasts.launcher.launcherError``. It costs 8 strings x 16 manual
    # locales, so it is a deliberate feature, not a silent wiring — filed
    # with the other i18n-cost items. Audit register item 4c.
