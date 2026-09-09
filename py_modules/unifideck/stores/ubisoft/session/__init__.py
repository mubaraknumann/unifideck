"""
Session sub-package — public exports.

Re-exports ``UbisoftSession``, the orchestration class for UPC session
state propagation between Wine prefixes.
"""

from .facade import UbisoftSession


def build_standalone_session() -> UbisoftSession:
    """A ``UbisoftSession`` wired from on-disk defaults alone.

    The normal session is built by ``stores/ubisoft/specialists.py`` from the
    live plugin's ``ConfigManager``. The out-of-process launcher has neither,
    but its last-resort prefix recovery still has to inject credentials — a
    prefix rebuilt without them makes UPC demand a sign-in for a user who is
    already signed in. Duplicating the credential logic there was the
    alternative, and this package already has four hand-rolled copies of the
    Ubisoft prefix resolver to show where that leads.

    Stdlib-only at import time (verified under system python 3.13), matching
    the launcher's constraint. Config lookups fall back to their defaults and
    log a "forgotten ConfigManager" notice — expected here, not a bug.
    """
    from unifideck.stores.ubisoft.config import UbisoftConfig
    from unifideck.stores.ubisoft.id_map import UbisoftIdMap
    from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths
    from unifideck.stores.ubisoft.prefix import UbisoftPrefixManager

    config = UbisoftConfig.from_config_manager(None)
    paths = UbisoftPrefixPaths(config)
    id_map = UbisoftIdMap(config, paths)
    # Same wiring as specialists.py: without the registry, prefixes installed
    # to SD / a custom base don't resolve.
    paths.set_prefix_registry(
        resolver=id_map.resolve_prefix_path,
        lister=id_map.all_prefix_paths,
    )
    return UbisoftSession(
        config=config,
        paths=paths,
        read_machine_guid=UbisoftPrefixManager.read_machine_guid,
    )

__all__ = ["UbisoftSession", "build_standalone_session"]
