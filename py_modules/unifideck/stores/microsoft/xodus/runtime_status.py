"""stores/microsoft/xodus/runtime_status.py — the components row of the status.

Compares the active components with the configured source:

* a local folder: ``outdated`` when its files differ (hashes the manifest);
* the GitHub release: ``outdated`` only when it offers a newer runtime or
  backend, so a newer local build is never replaced by an older release.
  The release list is cached (``release_source``), so this is cheap, and
  it is not read at all while Xbox PC is off.

A source that cannot be read leaves a usable install ``installed`` and
names the problem in ``error``. Blocking: call it from a worker thread.
"""
from __future__ import annotations

from typing import Any

from unifideck.launcher.xbox_pc.backend import check_backend
from unifideck.launcher.xbox_pc.layout import read_active

from .release_source import ReleaseSource
from .runtime import needs_install, release_is_newer
from .runtime_source import SourceError, source_from_config


def probe_runtime(source_value: Any, enabled: bool = True) -> dict[str, Any]:
    """The ``runtime`` row: state, version, where it looked, missing features."""
    view: dict[str, Any] = {"state": "missing", "version": None, "source_dir": "", "degraded": []}
    active = read_active()
    try:
        source = source_from_config(source_value)
        view["source_dir"] = source.where
        if isinstance(source, ReleaseSource):
            newer = enabled and active is not None and release_is_newer(source.offer(), active)
        else:
            listing = source.describe()
            newer = active is not None and needs_install(listing)
    except SourceError as e:
        view["error"], newer = e.code, False
    if active is None:
        return view
    view.update(
        version=active.runtime_version, backend_version=active.backend_version,
        degraded=list(active.degraded),
    )
    problem = check_backend(active)[1]
    if problem is not None:
        view.update(state="failed", error=problem)
    else:
        view["state"] = "outdated" if newer else "installed"
        view.pop("error", None)
    return view
