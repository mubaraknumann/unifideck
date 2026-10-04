# Feasibility Study: Local Vault and Remote Vault

**Decision (2026-09-24): two stores, built from today's GameVault store.**

- **Local Vault** imports any kind of game already on this device: archives, installed game
  folders, and games from other launchers.
- **Remote Vault** is GameVault renamed. It reaches every popular remote library: GameVault,
  RomM, Drop, and plain file shares, down to simple FTP.

**Verdict: feasible, and most of the machinery exists.** GameVault already separates *where games
come from* from *what we do with them*, and it already has a local mode. The work is:
- splitting that into two stores over one shared pipeline;
- adding sources to each;
- migrating the local-mode games that shipped in 0.7.5.

Games found on disk that belong to a store we already sync (a Heroic-installed Epic game) are
**not** Local Vault games. They are adopted by their own store; see [adoption.md](adoption.md).
The two share one piece: when the user runs an import scan, a found game that matches no store we
sync lands in Local Vault.

## What users asked for

| Issue | 👍 | Ask |
|---|---|---|
| #29 | 5 | Install from a self-hosted DRM-free server, naming RomM or Drop |
| #97 | 1 (+2 "me too") | Games already on disk, including a shared Windows/SteamOS partition. DRM-free games there are Local Vault; store games are [adoption](adoption.md) |

Nobody has asked for FTP, SMB or other launchers' imports yet. They are in scope because the user
chose a general design, not because of demand.

## What exists today

`stores/gamevault/` (2.6k LOC, local mode shipped in 0.7.5 via commit 4d475b89):

- `sources.py`: two narrow seams. `CatalogSource` answers "what games are there?".
  `ArchiveSource` answers "give me this game's archive, and take it back when done".
- `install.py`: one pipeline for every source (acquire, extract, find the exe with
  `stores/shared/launch_target.py`, write the marker, release). It has no mode branch.
- `library.py`: overlays install state; a failed read raises instead of returning a short list.
- `local_catalog.py`: folder of archives, ids `lv_<sha1 of title+year>`.
- `RemoteCatalog` in `library.py`: the GameVault server.
- `filename.py`: the `Title (v1.0) (W_P) (2021).zip` grammar, shared by both modes.
- `archive.py`: format by magic bytes, `bsdtar` first.

## Shape: two stores, one shared pipeline

Two stores now use the pipeline, so it moves to `stores/shared/` (the drift-guard "cross-store
helper" rule, plus a `SHARED_HELPERS` row in `scripts/validate_architecture.py`).

```
stores/shared/vault/               shared by both stores
    pipeline.py     acquire -> extract -> find exe -> marker -> release   (from gamevault/install.py)
    archive.py      (from gamevault)
    filename.py     title grammar (from gamevault)
    overlay.py      install-state overlay, raise-never-truncate           (from gamevault/library.py)
    seams.py        CatalogSource, ArchiveSource, InstalledSource

stores/gamevault/  "Remote Vault"  (store id unchanged; see Identity)
    store.py
    sources/gamevault.py   GameVault server API                           (moved)
    sources/romm.py        new
    sources/fileshare.py   new: FTP/FTPS, HTTP index, WebDAV
    sources/drop.py        later

stores/localvault/  "Local Vault"  (new store id)
    store.py
    sources/archives.py    folder of archives                              (from gamevault/local_catalog.py)
    sources/installed.py   new: folder of installed games
    sources/launchers.py   later: Heroic sideload, Lutris, Bottles, itch app
```

- **Each store can hold several sources.** For example, two RomM servers and an FTP share in
  Remote Vault. Game ids are namespaced per source (`romm:<server-hash>:<rom_id>`) so sources
  never collide and one can be removed cleanly.
- **A third seam for installed games.** `InstalledSource` returns a game already on disk; the
  pipeline skips acquire and extract and writes only the marker. Uninstall must **never** delete
  files the pipeline did not create. The marker records which case applies, and the check lives
  in the pipeline, not in each source. (Two stores once shared one install folder, and GOG's
  cleanup deleted the other store's files.)
- **Installers inside archives.** A known gap today: an archive holding `setup.exe` gets a shortcut
  to Setup. RomM PC entries, file shares and Humble all hit this, so build "run the installer in
  the prefix, then rescan for the real exe" once, in the shared pipeline.

## Identity and migration

- **Remote Vault keeps store id `gamevault`** and changes only its display name. Remote users
  need no migration.
- **Local Vault gets a new id (`localvault`)**, and local-mode games shipped in 0.7.5 must move to
  it. The move is unambiguous, because every local game id starts `lv_` and remote ids never do.

The cost: a shortcut's app id is `generate_app_id(launcher, "store:game_id")`, so changing the store
changes the app id. That would lose artwork, Steam playtime, collections and per-game settings.
The code already accepts a fixed id (`reconcile_phases.py:128`: `g.app_id or generate_app_id(...)`,
used by Battle.net). So the migration keeps each moved game's **old** app id.

What moves:
- the `lv_*` install markers from `gamevault_installed/`;
- the `vault_dir` from `gamevault_config.json` (mode `local`);
- the cached ids.

Run it once, idempotently, at startup, before the first sync. A failed migration must leave the old
entries alone rather than letting the next sync treat them as gone. A failed sync has deleted a
store's shortcuts before.

## Remote Vault sources

### GameVault: exists

Moves as it is. Fix the known cover-field bug on the way: `RemoteCatalog._extract_cover_url` reads
fields the GameVault API does not have. The real ones are `metadata.cover.source_url` and
`metadata.background.source_url`.

### RomM: feasible, PC entries only

- **Version:** 5.3.1 (2026-09-23), AGPL-3.0, about 13k stars. 5.3.0 changed the config format, so
  gate on the server version (`GET /api/heartbeat` returns `VERSION`).
- **Sign-in:**
  - The device-code flow (`POST /api/auth/device/init`, then poll `POST /api/auth/device/token`)
    or short-code pairing (`/api/client-tokens/exchange`). The user approves on another device and
    never types a password on the Deck. rommapp's own clients (Ludo, Argosy) use these.
  - HTTP Basic is the fallback.
- **Library:** `GET /api/platforms`, then `GET /api/roms?platform_ids=..&limit=..&offset=..`.
  Fields: `name`, `fs_name`, `platform_slug`, `fs_size_bytes`, `url_cover`, `path_cover_large`,
  `files[]`. Covers under `/assets/romm/resources/` load without auth.
- **Download:** `GET /api/roms/{id}/content/{file_name}`. Multi-file entries come back as a zip.
  Single files and cached zips resume with Range requests. The streamed-zip fallback may not (unverified).
- **Scope:** platforms `win`, `win3x`, `dos`, `linux` only. RomM has no launch metadata, so exe
  discovery is ours. DOS needs a DOSBox decision.
- **Out of scope:** emulated platforms. Tender, RomM-Dock and Ludo already put RomM ROMs into Steam
  on the Deck through RetroDECK or EmuDeck.

### File shares: FTP, HTTP index, WebDAV, then more

A file share is a Local Vault archive folder that happens to be remote. It reuses the same filename
grammar to name games. The only new parts are listing a directory and downloading a file over a
protocol.

| Protocol | How | Cost |
|---|---|---|
| FTP / FTPS | Python stdlib `ftplib` (`MLSD`/`NLST`, `REST` to resume) | none |
| HTTP directory index (nginx/Apache autoindex) | stdlib `urllib`, parse the index page, Range to resume | none |
| WebDAV (Nextcloud, many NAS boxes) | stdlib `urllib` with `PROPFIND` | none |
| SFTP | needs `paramiko` (native crypto; vendoring it for Decky's bundled Python 3.11 is the same class of problem as the vendored `cffi`) or rclone | medium |
| SMB | if the user has **mounted** it, it is already a local folder, so point Local Vault at it. Unmounted SMB needs `smbprotocol` or rclone | low / medium |
| S3, Google Drive, and so on | rclone | medium |

Start with the three stdlib protocols. They need no new dependency. If SFTP or cloud storage is
asked for, bundle **rclone** (MIT, v1.75.1, 31.5 MB zip) as one tool that covers them all, rather
than one Python library per protocol. It is an archive binary, so it follows the drift-guard
"archive bundled binary" row, like butler.

Credentials live in the store's config file with 0600 permissions, as GameVault's password does
today. Plain FTP sends the password unencrypted; the setup screen should say so and prefer FTPS.

### Drop: later

A self-hosted DRM-free platform with a real store protocol. Version 0.4.0 (2026-06-29), still
pre-release, AGPL-3.0 per its README.
- **Sign-in:** a certificate handshake (`POST /api/v1/client/auth/initiate`, browser sign-in,
  server-signed client key pair). After that, every request is a JWT signed by the client.
- **Downloads:** chunked and AES-128-CTR encrypted, with a manifest (delta updates) and a separate
  depot server.
- **Launch metadata:** it has it (`LaunchConfiguration.command`, platform, umu overrides), unlike RomM.
- Its own client already runs on the Deck through umu. Revisit at 1.0.

## Local Vault sources

### Archives: exists

Today's local mode, moved (`local_catalog.py`). Keeps its sentinel file and depth rules.

### Installed games: cheap

A folder where each subfolder is an installed game. Reuses the archive source's sentinel and depth
rules, skips extract, and picks the exe with `launch_target.py`. This covers #97's shared-partition
case for DRM-free games, a mounted SMB or NFS share, and any launcher we do not read directly.

### Other launchers: later

Heroic sideloaded games (`sideload_apps/library.json`), Lutris (`pga.db` `games` table plus
`games/<configpath>.yml`), Bottles (`library.yml`), itch app (`butler.db`). Each needs native and
Flatpak roots. Cartridges (kra-mo/cartridges, `cartridges/importer/`) is a clean reference for all
of them. Build when asked.

## Recommended order

1. **Split the stores.**
   - Move the pipeline to `stores/shared/vault/`.
   - Create Local Vault from local mode, with the `lv_*` migration that keeps old app ids.
   - Rename the remote side to Remote Vault.
   - No behaviour change: today's GameVault tests pass untouched, and it is validated on-device
     before anything is added.
2. **Remote Vault: RomM**, then **file shares** (FTP/FTPS, HTTP index, WebDAV).
3. **Local Vault: installed games**, the run-the-installer step, and the import scan's Local Vault
   outcome (games that match no synced store; see [adoption.md](adoption.md)).
4. Drop, rclone-backed protocols, other launchers: on request.

## Risks

- **Splitting a working store and migrating shipped data.** Move first, change second. Keep old app
  ids. Never let a failed migration look like "no games".
- **Uninstall of installed-folder games** must not delete files we did not create.
- **Protocol sprawl.** Every protocol is a support surface. The stdlib three are cheap; beyond
  them, one rclone beats many libraries.
- **RomM moves fast.** Pin a minimum server version.
- **Licensing.** RomM and Drop are AGPL servers; talking to their API is fine, copying code is not.
  Tender and Cartridges are GPL-3.0; use them for approach only.

## References

| Source | License | Use |
|---|---|---|
| rommapp/romm `backend/endpoints/`, `handler/auth/` | AGPL-3.0 | API shape, auth flows |
| rommapp/ludo, rommapp/argosy-launcher | see repos | device-code and pairing clients |
| danielcopper/romm-tender | GPL-3.0 | existing Decky RomM integration (ROMs) |
| Drop-OSS/drop `server/internal/clients/README.md`, torrential | AGPL-3.0 | client auth, depot protocol |
| rclone/rclone | MIT | optional multi-protocol backend |
| kra-mo/cartridges `cartridges/importer/` | GPL-3.0 | multi-launcher importer reference |
