"""
UPC launch environment builder — assembles the env dict for wine-running UPC.

Two thin dataclasses (``UbisoftInstallerLaunchEnv``,
``UbisoftLauncherLaunchEnv``) describe the environment variables and
Wine prefix configuration needed by the installer launcher and the
game launcher respectively.

The split between installer/game env exists because the installer needs
``WINEDLLOVERRIDES=mshtml=`` to bypass the embedded IE component, while
games need ``DXVK_*`` overrides for Wine D3D — keeping the two envs
separate avoids leaking installer-only overrides into game launches.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _UpcLaunchEnv:
    """Upc launch env."""

    upc_path: str
    umu_run: str
    python_bin: str
    env: dict[str, str]

class UpcLaunchEnvBuildError(Exception):
    """Upc launch env build error."""

    def __init__(self, error_code: str) -> None:
        """Initialize the instance."""
        super().__init__(error_code)
        self.error_code = error_code
