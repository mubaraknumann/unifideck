"""start_comet: environment handed to comet, and a visible start failure."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.core.paths import comet_config_file, comet_config_home
from unifideck.launcher.proton.compat import gog


class _Proc:
    def __init__(self, rc: int | None) -> None:
        self.pid = 4242
        self._rc = rc

    def wait(self, timeout: float | None = None) -> int:
        if self._rc is None:
            raise subprocess.TimeoutExpired("comet", timeout or 0)
        return self._rc


@pytest.fixture
def launch(tmp_path: Path, monkeypatch):
    """Run start_comet with a fake binary + tokens; returns (result, popen kwargs)."""
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    (plugin / "bin" / "comet").write_text("")
    monkeypatch.setattr(gog, "_read_gog_tokens", lambda: ("acc", "ref", "42"))
    plan: Any = SimpleNamespace(context=SimpleNamespace(plugin_dir=plugin))

    def run(rc: int | None) -> tuple[Any, dict[str, Any]]:
        seen: dict[str, Any] = {}

        def fake_popen(args: list[str], **kw: Any) -> _Proc:
            seen.update(kw, args=args)
            return _Proc(rc)

        monkeypatch.setattr(gog.subprocess, "Popen", fake_popen)
        return gog.start_comet(plan), seen

    return run


def test_running_comet_is_returned(launch) -> None:
    proc, _ = launch(None)
    assert proc is not None and proc.pid == 4242


def test_comet_dying_at_start_is_reported_and_dropped(launch, caplog) -> None:
    proc, _ = launch(127)
    assert proc is None
    assert "achievements and online features are OFF" in caplog.text
    assert "glibc 2.38" in caplog.text


def test_push_config_redirects_only_xdg_config(launch) -> None:
    _, seen = launch(None)
    assert "XDG_CONFIG_HOME" not in seen["env"]

    comet_config_file().parent.mkdir(parents=True)
    comet_config_file().write_text("[[custom_event]]\n")
    _, seen = launch(None)
    assert seen["env"]["XDG_CONFIG_HOME"] == str(comet_config_home())
    assert "XDG_DATA_HOME" not in seen["env"]
    assert seen["env"]["COMET_IDLE_WAIT"] == str(gog.COMET_IDLE_WAIT_SECONDS)
