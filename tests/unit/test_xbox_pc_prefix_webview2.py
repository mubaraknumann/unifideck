"""The runtime's WebView2 in a game prefix (ISSUES.md I-041)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from unifideck.launcher.xbox_pc import prefix as prefix_mod
from unifideck.launcher.xbox_pc import prefix_webview2 as wv2
from unifideck.launcher.xbox_pc.errors import XboxPcLaunchError
from unifideck.launcher.xbox_pc.runtime import Runtime

VERSION = "155.0.4283.62"
INSTALLED_SYSTEM_REG = """WINE REGISTRY Version 2

[Software\\\\Wow6432Node\\\\Microsoft\\\\EdgeUpdate\\\\ClientState\\\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}] 1792598328
#time=1dd61750f1400c2
"brand"="GGLS"
"EBWebView"="C:\\\\Program Files (x86)\\\\Microsoft\\\\EdgeWebView\\\\Application\\\\155.0.4283.62"

[Software\\\\Wow6432Node\\\\Microsoft\\\\EdgeUpdate\\\\Clients\\\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}] 1792598313
#time=1dd6175060ea356
"location"="C:\\\\Program Files (x86)\\\\Microsoft\\\\EdgeWebView\\\\Application"
"name"="Microsoft Edge WebView2 Runtime"
"pv"="155.0.4283.62"
"""


def _runtime(root: Path, *versions: str, feature: bool = True) -> Runtime:
    for version in versions:
        folder = root / "files/share/webview2" / version
        folder.mkdir(parents=True)
        (folder / "msedgewebview2.exe").write_bytes(b"MZ")
    return Runtime(root, [wv2.WEBVIEW2_FEATURE] if feature else [], {})


def test_shared_webview2_needs_the_feature_and_one_version(tmp_path: Path) -> None:
    assert wv2.shared_webview2(_runtime(tmp_path / "a", VERSION, feature=False)) is None
    runtime = _runtime(tmp_path / "b", VERSION)
    assert wv2.shared_webview2(runtime) == (VERSION, runtime.files / "share/webview2")
    with pytest.raises(XboxPcLaunchError) as err:
        wv2.shared_webview2(_runtime(tmp_path / "c", VERSION, "156.0.1.2"))
    assert err.value.reason == "runtime_missing"
    with pytest.raises(XboxPcLaunchError):
        wv2.shared_webview2(Runtime(tmp_path / "d", [wv2.WEBVIEW2_FEATURE], {}))


def test_link_points_at_the_current_runtime_and_spares_a_real_install(tmp_path: Path) -> None:
    old, new = tmp_path / "rt/0.1.1/files/share/webview2", tmp_path / "rt/0.1.2/files/share/webview2"
    prefix = tmp_path / "pfx"
    assert wv2.link_webview2(prefix, old)
    assert wv2.link_webview2(prefix, new)
    assert Path(os.readlink(prefix / wv2.APPLICATION_DIR)) == new
    real = tmp_path / "pfx2" / wv2.APPLICATION_DIR
    real.mkdir(parents=True)
    assert not wv2.link_webview2(tmp_path / "pfx2", new)
    assert real.is_dir() and not real.is_symlink()


def test_stale_values_read_system_reg(tmp_path: Path) -> None:
    assert len(wv2.stale_values(tmp_path, VERSION)) == 3
    (tmp_path / "system.reg").write_text(INSTALLED_SYSTEM_REG)
    assert wv2.stale_values(tmp_path, VERSION) == []
    # A runtime update changes the version: pv and EBWebView go stale, location does not.
    assert [name for _, name, _ in wv2.stale_values(tmp_path, "156.0.1.2")] == ["pv", "EBWebView"]


def test_reg_file_text_groups_values_by_key() -> None:
    text = wv2.reg_file_text(wv2.wanted_values(VERSION))
    assert text.startswith("Windows Registry Editor Version 5.00\r\n")
    assert "[HKEY_LOCAL_MACHINE\\Software\\Wow6432Node\\Microsoft\\EdgeUpdate\\Clients\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}]" in text
    assert '"EBWebView"="C:\\\\Program Files (x86)\\\\Microsoft\\\\EdgeWebView\\\\Application\\\\155.0.4283.62"' in text
    assert text.count("[HKEY_LOCAL_MACHINE") == 2


def _recording_proton(runtime: Runtime, *, rc: int) -> None:
    """``proton`` appends its argv and the .reg text it was handed to ``calls.jsonl``."""
    proton = runtime.root / "proton"
    proton.write_text("\n".join([
        f"#!{sys.executable}",
        "import json, os, pathlib, sys",
        "data = pathlib.Path(os.environ['STEAM_COMPAT_DATA_PATH'])",
        "reg = data / 'drive_c/windows/temp/unifideck-webview2.reg'",
        "text = reg.read_text(encoding='utf-16') if reg.exists() else None",
        "with (data / 'calls.jsonl').open('a') as f: f.write(json.dumps([sys.argv[1:], text]) + '\\n')",
        "print('regedit went wrong')",
        f"sys.exit({rc})",
    ]) + "\n")
    proton.chmod(0o755)


@pytest.mark.asyncio
async def test_install_webview2_links_and_imports_once(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "runtime", VERSION)
    _recording_proton(runtime, rc=0)
    compat = tmp_path / "pfx"
    compat.mkdir()
    env = {"STEAM_COMPAT_DATA_PATH": str(compat), "PATH": os.environ["PATH"]}
    await prefix_mod._install_webview2(runtime, compat, env, 30.0)
    assert Path(os.readlink(compat / wv2.APPLICATION_DIR)) == runtime.files / "share/webview2"
    [(argv, text)] = [json.loads(line) for line in (compat / "calls.jsonl").read_text().splitlines()]
    assert argv == ["run", "regedit", "/S", "C:\\windows\\temp\\unifideck-webview2.reg"]
    assert '"pv"="155.0.4283.62"' in text
    assert not (compat / "drive_c/windows/temp/unifideck-webview2.reg").exists()
    # Once Wine has saved the values, nothing runs.
    (compat / "system.reg").write_text(INSTALLED_SYSTEM_REG)
    await prefix_mod._install_webview2(runtime, compat, env, 30.0)
    assert len((compat / "calls.jsonl").read_text().splitlines()) == 1


@pytest.mark.asyncio
async def test_install_webview2_failure_is_prefix_failed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "runtime", VERSION)
    _recording_proton(runtime, rc=1)
    compat = tmp_path / "pfx"
    compat.mkdir()
    env = {"STEAM_COMPAT_DATA_PATH": str(compat), "PATH": os.environ["PATH"]}
    with pytest.raises(XboxPcLaunchError) as err:
        await prefix_mod._install_webview2(runtime, compat, env, 30.0)
    assert err.value.reason == "prefix_failed"
    assert "WebView2 155.0.4283.62" in err.value.detail and "regedit went wrong" in err.value.detail
