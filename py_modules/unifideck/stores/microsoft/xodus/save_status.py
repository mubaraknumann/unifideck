"""stores/microsoft/xodus/save_status.py — compare Xbox PC saves with the cloud.

The Xodus runtime syncs these saves inside the game. This module only reads,
so the game page can say whether a save is ready for another device or for
xCloud, and Uninstall can warn before deleting one that never uploaded (I-040).

**Unifideck never pushes a save.** Only the runtime uploads. Every call to the
save service goes through :func:`_get`, which can only send ``GET``, and the
service's lock is never touched: taking it would block other devices.

The states follow what the runtime does at its next launch (``cloud_sync`` and
``sync_local_callback`` in the xgameruntime save provider):

* the cloud time is compared as a string with the index's ``remote`` line;
* a changed cloud container downloads unless the local one is dirty and newer;
* a local-only container uploads when dirty or never uploaded. A clean one was
  deleted in the cloud, so the runtime deletes the local copy too.

Measured 2026-11-06: a user-only ``xboxlive.com`` XSTS token is accepted, with
no device token and no signature. ``x-xbl-pfn`` is required (403 without it).
An unknown SCID gives 400, and a valid one with no saves gives 404.

The SCID is read from the save folder, so a game that has not run under this
account has nothing to compare.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from unifideck.core.safe_delete import canonical_prefix
from unifideck.launcher.xbox_pc import xml_lite
from unifideck.launcher.xbox_pc.component_locks import game_lock_path
from unifideck.launcher.xbox_pc.game_manifest import MANIFEST_NAME, find_ci
from unifideck.stores.microsoft.tokens.endpoint import HttpReply, request_json

if TYPE_CHECKING:
    from unifideck.stores.microsoft.tokens.xbl_chain import XBLTokenChain

logger = logging.getLogger(__name__)

SAVE_FOLDER = "drive_c/users/steamuser/AppData/Local/XodusGameSave"
_STORAGE = "https://titlestorage.xboxlive.com/connectedstorage/users/xuid({xuid})/scids/{scid}"
_CONTRACT_VERSION = "107"
_CONTAINER_SUFFIX = ",savedgame"
_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
_ATTEMPTS = 3
_RETRY_WAIT_S = 0.5
_TIMEOUT_S = 15.0
_MANIFEST_WORKERS = 4
#: An index is a few short lines per blob. Anything far larger is not one.
_MAX_INDEX_BYTES = 1024 * 1024

IN_SYNC = "in_sync"
WAITING_UPLOAD = "waiting_upload"
CLOUD_NEWER = "cloud_newer"
CLOUD_REMOVED = "cloud_removed"
BOTH_CHANGED = "both_changed"
UNKNOWN = "unknown"
NO_SAVES = "no_saves"
NOT_LAUNCHED = "not_launched"
RUNNING = "running"
OFFLINE = "offline"

#: Worst first. A game shows the worst state of any of its containers.
_SEVERITY = (BOTH_CHANGED, CLOUD_REMOVED, CLOUD_NEWER, WAITING_UPLOAD, UNKNOWN, IN_SYNC)


@dataclass(frozen=True)
class LocalSave:
    """One container's ``index`` file."""

    remote: str
    dirty: bool
    #: Hex blob name to atom id.
    blobs: dict[str, str]

    @property
    def not_uploaded(self) -> bool:
        """The cloud lacks this copy: it changed here, or never went up."""
        return self.dirty or not self.remote


@dataclass
class SaveFolder:
    """The containers of one SCID."""

    saves: dict[str, LocalSave] = field(default_factory=dict)
    #: Indexes that could not be read. Never touched: the broken state is the evidence.
    malformed: int = 0

    @property
    def not_uploaded(self) -> int:
        """Containers the cloud may lack. An unreadable one counts: it may hold a save."""
        return self.malformed + sum(save.not_uploaded for save in self.saves.values())


def _parse_index(text: str, folder_name: str) -> tuple[str, LocalSave] | None:
    """``(container name, save)``, or None when the index is not one."""
    fields: dict[str, str] = {}
    blobs: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "blob":
            parts = value.split()
            if len(parts) != 3:
                return None
            blobs[parts[0]] = parts[2]
        else:
            fields[key] = value
    if "remote" not in fields or "dirty" not in fields:
        return None
    try:
        # File-save titles name their folder differently and store the name.
        name = bytes.fromhex(fields.get("name", folder_name)).decode()
    except ValueError:
        return None
    remote = "" if fields["remote"] == "-" else fields["remote"]
    return name, LocalSave(remote=remote, dirty=fields["dirty"].strip() != "0", blobs=blobs)


def _read_container(folder: Path) -> tuple[str, LocalSave] | None:
    try:
        with (folder / "index").open("rb") as stream:
            raw = stream.read(_MAX_INDEX_BYTES)
    except OSError as err:
        logger.warning("[XboxSaves] cannot read %s: %s", folder / "index", err)
        return None
    parsed = _parse_index(raw.decode("utf-8", "replace"), folder.name)
    if parsed is None:
        logger.warning("[XboxSaves] unreadable save index in %s", folder)
    return parsed


def _read_folder(scid_dir: Path) -> SaveFolder:
    found = SaveFolder()
    for entry in _subdirs(scid_dir):
        parsed = _read_container(entry)
        if parsed is None:
            found.malformed += 1
        else:
            found.saves[parsed[0]] = parsed[1]
    return found


def _subdirs(path: Path) -> list[Path]:
    try:
        return [Path(entry.path) for entry in os.scandir(path) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def read_local(prefix: Path) -> dict[str, dict[str, SaveFolder]]:
    """``{xuid: {scid: folder}}`` from a prefix. Read-only. Blocking."""
    root = prefix / SAVE_FOLDER
    return {
        account.name: {scid.name: _read_folder(scid) for scid in _subdirs(account)}
        for account in _subdirs(root)
    }


def _account_not_uploaded(folders: dict[str, SaveFolder]) -> int:
    return sum(folder.not_uploaded for folder in folders.values())


def count_not_uploaded(pid: str) -> int:
    """Containers in the prefix the cloud may lack, for every account. Blocking."""
    return sum(_account_not_uploaded(folders) for folders in read_local(canonical_prefix(pid)).values())


def has_local_saves(pid: str) -> bool:
    """Whether the prefix holds any save container. Blocking."""
    root = canonical_prefix(pid) / SAVE_FOLDER
    return any(
        _subdirs(scid)
        for account in _subdirs(root)
        for scid in _subdirs(account)
    )


def game_is_running(pid: str) -> bool:
    """Whether something holds the game's lock (a launch, update or uninstall).

    Reads ``/proc/locks`` rather than trying the lock: a launch that met even a
    brief hold from us would refuse with ``already_running``. Blocking.
    """
    try:
        inode = game_lock_path(pid).stat().st_ino
        with open("/proc/locks", encoding="ascii", errors="replace") as stream:
            lines = stream.readlines()
    except OSError:
        return False
    return any(_lock_line_inode(line) == inode for line in lines)


def _lock_line_inode(line: str) -> int | None:
    """The inode of a ``/proc/locks`` line, or None."""
    parts = line.split()
    if len(parts) < 6 or parts[1] != "FLOCK":
        return None
    try:
        return int(parts[5].rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def package_family(game_dir: Path) -> str | None:
    """Windows' package family name, from the game's own config, or None. Blocking."""
    config = find_ci(game_dir, MANIFEST_NAME)
    if config is None:
        return None
    try:
        root = xml_lite.parse_file(config)
    except (OSError, xml_lite.ParseError):
        return None
    for node in root.iter():
        if node.tag == "Identity":
            return _family_name(node.get("Name") or "", node.get("Publisher") or "")
    return None


def _family_name(name: str, publisher: str) -> str | None:
    if not name or not publisher:
        return None
    digest = hashlib.sha256(publisher.encode("utf-16-le")).digest()[:8]
    # 64 bits padded to 65, written as 13 base32 digits.
    bits = int.from_bytes(digest, "big") << 1
    published = "".join(_CROCKFORD[(bits >> (5 * i)) & 31] for i in range(12, -1, -1))
    return f"{name}_{published}"


def _get(chain: XBLTokenChain, pfn: str, scid: str, path: str = "") -> HttpReply:
    """The only call to the save service. Always ``GET``, never the lock."""
    url = _STORAGE.format(xuid=chain.xuid, scid=scid) + path
    headers = {
        "Authorization": f"XBL3.0 x={chain.user_hash};{chain.xsts_token}",
        "x-xbl-contract-version": _CONTRACT_VERSION,
        "x-xbl-pfn": pfn,
    }
    reply = HttpReply(None)
    for attempt in range(_ATTEMPTS):
        if attempt:
            time.sleep(_RETRY_WAIT_S * attempt)
        reply = request_json("GET", url, headers, timeout=_TIMEOUT_S)
        if reply.status is not None and reply.status < 500 and reply.status != 429:
            break
    return reply


def _cloud_times(body: dict[str, Any]) -> dict[str, str]:
    """``{container name: cloud time}``, skipping what the runtime skips."""
    times: dict[str, str] = {}
    blobs = body.get("blobs")
    for blob in blobs if isinstance(blobs, list) else []:
        name = blob.get("fileName") if isinstance(blob, dict) else None
        if isinstance(name, str) and name.endswith(_CONTAINER_SUFFIX):
            times[name[: -len(_CONTAINER_SUFFIX)]] = str(blob.get("clientFileTime") or "")
    return times


def _list_cloud(chain: XBLTokenChain, pfn: str, scid: str) -> tuple[dict[str, str], str | None]:
    """``(containers, problem)``. Problem: None, ``network``, ``refused`` or ``paged``."""
    reply = _get(chain, pfn, scid)
    if reply.status == 404:
        return {}, None
    if reply.status is None or reply.status >= 500 or reply.status == 429:
        logger.info("[XboxSaves] no answer for a save listing: %s", reply.detail or reply.status)
        return {}, "network"
    if reply.status != 200:
        logger.warning("[XboxSaves] save listing refused: HTTP %s", reply.status)
        return {}, "refused"
    paging = reply.body.get("pagingInfo")
    if isinstance(paging, dict) and paging.get("continuationToken"):
        logger.warning("[XboxSaves] save listing has more pages than we read")
        return {}, "paged"
    return _cloud_times(reply.body), None


def _cloud_atoms(chain: XBLTokenChain, pfn: str, scid: str, name: str) -> dict[str, str] | None:
    """``{hex blob name: atom id}`` of a cloud container, or None."""
    path = "/" + quote(name, safe="") + _CONTAINER_SUFFIX
    reply = _get(chain, pfn, scid, path)
    atoms = reply.body.get("atoms") if reply.status == 200 else None
    if not isinstance(atoms, dict):
        return None
    return {str(key).encode().hex(): str(atom) for key, atom in atoms.items()}


def container_state(
    cloud_time: str | None, local: LocalSave | None, atoms: dict[str, str] | None = None,
) -> str:
    """One container's state. ``atoms`` is the cloud manifest, for an equal time."""
    if local is None:
        return CLOUD_NEWER
    if cloud_time is None:
        return WAITING_UPLOAD if local.not_uploaded else CLOUD_REMOVED
    if cloud_time != local.remote:
        return BOTH_CHANGED if local.dirty else CLOUD_NEWER
    if local.dirty:
        return WAITING_UPLOAD
    return IN_SYNC if atoms is not None and atoms == local.blobs else UNKNOWN


def worst_state(states: list[str]) -> str:
    """The worst of ``states``. No containers at all is ``no_saves``."""
    for state in _SEVERITY:
        if state in states:
            return state
    return NO_SAVES


def _folder_states(
    chain: XBLTokenChain, pfn: str, scid: str, folder: SaveFolder, cloud: dict[str, str],
) -> list[str]:
    """Compare one SCID. Manifests are read only where the times already agree."""
    agree = [
        name for name, save in folder.saves.items()
        if not save.dirty and cloud.get(name) == save.remote
    ]
    with ThreadPoolExecutor(max_workers=_MANIFEST_WORKERS) as pool:
        atoms = dict(zip(agree, pool.map(lambda n: _cloud_atoms(chain, pfn, scid, n), agree), strict=True))
    states = [UNKNOWN] * folder.malformed
    for name in set(cloud) | set(folder.saves):
        state = container_state(cloud.get(name), folder.saves.get(name), atoms.get(name))
        states.append(state)
        if state != IN_SYNC:
            logger.info("[XboxSaves] container %s: %s", name, state)
    return states


def _result(state: str, waiting: int) -> dict[str, Any]:
    return {"applicable": True, "state": state, "waiting": waiting, "checked_at": time.time()}


def _offline(waiting: int) -> dict[str, Any]:
    """Being offline does not change what the index says about this Deck."""
    return _result(WAITING_UPLOAD if waiting else OFFLINE, waiting)


def check_saves(chain: XBLTokenChain | None, pid: str, game_dir: Path) -> dict[str, Any]:
    """The game's save status. Network and disk: run it off the event loop."""
    accounts = read_local(canonical_prefix(pid))
    if chain is None or not chain.xuid:
        return _offline(sum(_account_not_uploaded(folders) for folders in accounts.values()))
    # Another account's saves are not this account's. Its cloud is not either.
    folders = accounts.get(chain.xuid)
    if not folders:
        return _result(NOT_LAUNCHED, 0)
    waiting = _account_not_uploaded(folders)
    pfn = package_family(game_dir)
    if pfn is None:
        logger.warning("[XboxSaves] no package identity in %s", game_dir)
        return _result(WAITING_UPLOAD if waiting else UNKNOWN, waiting)
    states: list[str] = []
    for scid, folder in folders.items():
        cloud, problem = _list_cloud(chain, pfn, scid)
        if problem == "network":
            return _offline(waiting)
        if problem is not None:
            # A folder with no saves can be left by a SCID the service refuses.
            if folder.saves or folder.malformed:
                states.append(UNKNOWN)
            continue
        states += _folder_states(chain, pfn, scid, folder, cloud)
    return _result(worst_state(states), waiting)
