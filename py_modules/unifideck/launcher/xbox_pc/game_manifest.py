"""launcher/xbox_pc/game_manifest.py — what an installed game folder says.

An Xbox PC game folder holds ``MicrosoftGame.config`` (the GDK package
manifest) and, once the download is complete, ``.xodus-streaming.msixvc``.
The manifest names the launch executable, the declared languages, the
VC++ dependency and the processor architecture. GDK titles are authored on
Windows, so the manifest's file name casing varies (I-010).

Port of the spike's ``inspect_game`` (``xbox-pc-spike/launch/launch.py``).
Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import xml_lite
from .errors import XboxPcLaunchError
from .xml_lite import Element

MANIFEST_NAME = "MicrosoftGame.config"
CONTAINER_NAME = ".xodus-streaming.msixvc"
APPX_MANIFEST_NAME = "appxmanifest.xml"
_VC_REDIST = "Engine/Extras/Redist/en-us/vc_redist.x64.exe"
_VCLIBS_PREFIX = "Microsoft.VCLibs.140.00"
_EAC_DIRS = ("EasyAntiCheat", "EasyAntiCheat_EOS")
_EAC_MARKERS = (["Settings.json"], ["Launcher", "Settings.json"])
_BATTLEYE_DIRS = ("BattlEye",)
_BATTLEYE_MARKERS = (
    ["BEClient_x64.dll"], ["BEClient.dll"], ["BEService_x64.exe"], ["BEService.exe"],
)
_LOCALE_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
_HEX_RE = re.compile(r"[0-9A-Fa-f]+")


@dataclass(frozen=True)
class GameInfo:
    """The facts a launch needs from one installed game folder."""

    path: Path
    title: str
    executable: str
    store_id: str | None
    title_id: str | None
    locales: tuple[str, ...]
    vc_redist_encrypted: bool
    eac: str | None
    container_ready: bool
    #: ``<ProcessorArchitecture>``, lower-case (``x86`` for a 32-bit game),
    #: or None when the manifest does not say. The executables are
    #: encrypted on disk, so their PE header cannot be read instead.
    architecture: str | None = None
    #: The game's BattlEye folder relative to ``path``, or None.
    battleye: str | None = None
    #: The config lists ``KnownDependency VC14`` or the appx manifest the
    #: ``Microsoft.VCLibs.140.00`` framework: the game wants Microsoft's
    #: VC++ DLLs, not Wine's builtins (I-047).
    needs_vc_runtime: bool = False

    @property
    def exe_name(self) -> str:
        """The executable's file name, for ``taskkill /IM``."""
        return re.split(r"[\\/]", self.executable)[-1]


def _local(tag: str) -> str:
    """An XML tag without its namespace."""
    return tag.rsplit("}", 1)[-1]


def find_ci(directory: Path, name: str) -> Path | None:
    """The entry of ``directory`` named ``name`` in any casing, or None."""
    lowered = name.lower()
    try:
        for entry in directory.iterdir():
            if entry.name.lower() == lowered:
                return entry
    except OSError:
        return None
    return None


def _begins_with_mz(path: Path) -> bool:
    """Whether ``path`` starts like a Windows executable."""
    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"MZ"
    except OSError:
        return False


def _first(nodes: list[Element], tag: str) -> Element | None:
    """The first node named ``tag``, or None."""
    return next((n for n in nodes if _local(n.tag) == tag), None)


def _text(node: Element | None) -> str:
    """A node's stripped text, or ``""``."""
    return (node.text or "").strip() if node is not None else ""


def _locales(nodes: list[Element]) -> tuple[str, ...]:
    """The well-formed languages the manifest declares, sorted."""
    found = {
        n.get("Language", "") for n in nodes
        if _local(n.tag) == "Resource" and _LOCALE_RE.fullmatch(n.get("Language", ""))
    }
    return tuple(sorted(found))


def _needs_vc(nodes: list[Element]) -> bool:
    """Whether the manifest lists the VC14 runtime as a dependency."""
    return any(
        _local(n.tag) == "KnownDependency" and (n.get("Name") or "").upper() == "VC14"
        for n in nodes
    )


def _appx_needs_vc(root: Path) -> bool:
    """Whether ``appxmanifest.xml`` depends on the ``Microsoft.VCLibs.140.00`` framework."""
    appx = find_ci(root, APPX_MANIFEST_NAME)
    if appx is None:
        return False
    try:
        nodes = xml_lite.parse_file(appx).iter()
    except (OSError, xml_lite.ParseError):
        return False
    return any(
        _local(n.tag) == "PackageDependency" and (n.get("Name") or "").startswith(_VCLIBS_PREFIX)
        for n in nodes
    )


def _vc_redist_encrypted(nodes: list[Element], root: Path) -> bool:
    """Whether the game needs VC++ and ships its installer encrypted."""
    vc_path = root / _VC_REDIST
    return _needs_vc(nodes) and vc_path.is_file() and not _begins_with_mz(vc_path)


def _vc_facts(nodes: list[Element], root: Path) -> tuple[bool, bool]:
    """``(needs VC++, ships its VC++ installer encrypted)``."""
    return _needs_vc(nodes) or _appx_needs_vc(root), _vc_redist_encrypted(nodes, root)


def _find_ci_path(root: Path, parts: list[str]) -> Path | None:
    """``root`` joined with ``parts``, each matched in any casing, or None."""
    current = root
    for part in parts:
        found = find_ci(current, part)
        if found is None:
            return None
        current = found
    return current


def _anticheat_dir(
    root: Path, executable: str, names: tuple[str, ...], markers: tuple[list[str], ...],
) -> str | None:
    """An anti-cheat folder of the game, relative to ``root``, or None.

    The folder is one of ``names``, at the game root or next to the launch
    executable, and holds one of ``markers`` (paths inside it), all in any
    casing. EasyAntiCheat keeps ``Settings.json`` either directly or in
    ``Launcher/``. A missed folder runs the anti-cheat without Steam's
    Proton runtime for it, and an EAC game then restarts itself without end
    (I-044).
    """
    exe_parts = [p for p in re.split(r"[\\/]", executable) if p][:-1]
    for base in dict.fromkeys(filter(None, (root, _find_ci_path(root, exe_parts)))):
        for name in names:
            folder = find_ci(base, name)
            if folder is None:
                continue
            found = (_find_ci_path(folder, parts) for parts in markers)
            if any(path is not None and path.is_file() for path in found):
                return folder.relative_to(root).as_posix()
    return None


def _identity(nodes: list[Element], root: Path) -> tuple[str, str | None, str | None]:
    """``(display title, StoreId, hex TitleId)`` from the manifest."""
    visuals = _first(nodes, "ShellVisuals")
    store_node = _first(nodes, "StoreId")
    title_id = _text(_first(nodes, "TitleId"))
    title = (visuals.get("DefaultDisplayName") if visuals is not None else None) or root.name
    return (
        title,
        _text(store_node) if store_node is not None else None,
        title_id if _HEX_RE.fullmatch(title_id) else None,
    )


def _executable(nodes: list[Element]) -> str:
    """The launch executable the manifest names. Raises ``not_downloaded``."""
    exe_node = _first(nodes, "Executable")
    executable = exe_node.get("Name") if exe_node is not None else None
    if not executable:
        raise XboxPcLaunchError("not_downloaded", "the manifest names no executable")
    return executable


def _architecture(nodes: list[Element]) -> str | None:
    """``<ProcessorArchitecture>`` lower-cased, or None when absent."""
    return _text(_first(nodes, "ProcessorArchitecture")).lower() or None


def _parse_manifest(root: Path) -> list[Element]:
    """Every node of the folder's manifest. Raises ``not_downloaded``."""
    config = find_ci(root, MANIFEST_NAME)
    if config is None:
        raise XboxPcLaunchError("not_downloaded", f"{root} has no {MANIFEST_NAME}")
    try:
        # xml_lite, not xml.etree: the plugin imports this module too, and
        # Decky's frozen Python has no xml package. It refuses a DOCTYPE,
        # so no entity can be declared.
        return list(xml_lite.parse_file(config).iter())
    except (OSError, xml_lite.ParseError) as err:
        raise XboxPcLaunchError("not_downloaded", f"unreadable {config.name}: {err}") from err


def inspect_game(directory: Path) -> GameInfo:
    """Read ``directory``'s manifest. Raises ``not_downloaded`` when it has none.

    Blocking file I/O: call it through ``asyncio.to_thread``.
    """
    root = directory.resolve()
    nodes = _parse_manifest(root)
    title, store_id, title_id = _identity(nodes, root)
    executable = _executable(nodes)
    needs_vc, vc_encrypted = _vc_facts(nodes, root)
    return GameInfo(
        path=root,
        title=title,
        executable=executable,
        store_id=store_id,
        title_id=title_id,
        locales=_locales(nodes),
        vc_redist_encrypted=vc_encrypted,
        eac=_anticheat_dir(root, executable, _EAC_DIRS, _EAC_MARKERS),
        container_ready=(root / CONTAINER_NAME).is_file(),
        architecture=_architecture(nodes),
        battleye=_anticheat_dir(root, executable, _BATTLEYE_DIRS, _BATTLEYE_MARKERS),
        needs_vc_runtime=needs_vc,
    )
