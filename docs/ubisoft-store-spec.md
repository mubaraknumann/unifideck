# Ubisoft Store Integration — Spec (v2)

> **Status:** current as of 2026-06-22 (branch `for-pr-0.7`). This v2 spec documents the
> Ubisoft integration **as actually implemented** in the `stores/ubisoft/` package. It
> replaces the original March-2026 design spec (retained locally under `docs/archive/`,
> outside version control). Two major divergences from that v1 design: authentication is
> **shortcut-launched Ubisoft Connect** (not a direct REST/2FA flow), and the library is
> built by **parsing UPC's local binary catalogs** (not a GraphQL API).

---

## 1. Overview

Ubisoft games run through **Ubisoft Connect (UPC)** under Proton — there is no public
download/launch API we can drive headlessly, so the integration *orchestrates the real
UPC client* inside Wine prefixes rather than reimplementing it:

- **Auth** — the user signs into UPC's own GUI, launched as a Steam shortcut in a
  dedicated auth prefix. We watch for the credential files UPC writes and propagate them.
- **Library** — read from UPC's on-disk binary catalogs (`configurations` + `ownership`)
  plus an installed-game scan; no network catalog call (an optional public free-to-play
  CDN feed can supplement it).
- **Install** — UPC drives the actual download; the backend launches UPC, then watches
  the filesystem and reports an indeterminate "Installing in Ubisoft Connect" state.
- **Launch** — `upc.exe uplay://launch/{id}/0` inside the per-game prefix.

Unlike Epic/GOG/Amazon (which use `legendary`/`gogdl`/`nile` CLIs), the entire Ubisoft
flow is **local-file + real-UPC** driven.

---

## 2. Package Layout

All paths under `py_modules/unifideck/stores/ubisoft/`.

| Module | Responsibility |
| ------ | -------------- |
| `store.py` | `UbisoftStore(StoreBase)` — facade; implements the store contract and delegates to specialists |
| `specialists.py` | Builds and wires the specialist objects (config, paths, binaries, id-map, session, installer, prefix manager, library, auth) |
| `config.py` | `UbisoftConfig` — paths, prefix names, install bases, feature flags (from `stores.ubisoft.*`) |
| `paths.py` | Per-game prefix resolution, UPC/Connect exe discovery, prefix layout probing |
| `binaries.py` | umu/Proton/Python discovery, launch-environment construction |
| `id_map.py` | `ubisoft_id_map.json` read/write: `space_id` ↔ install/launch/connect IDs ↔ recorded prefix path |
| `id_map_sources.py` | Extract IDs from the Wine `system.reg`; community game-ID DB fetch/parse |
| `leveldb_ids.py` | Object-scoped `spaceId` ↔ `ubisoftConnectGameId` scan of UPC's localStorage leveldb, plus the duplicate-ID guard |
| `parser.py` | Parse UPC's binary `configuration/configurations` and `ownership/{userId}` |
| `parser_binary.py` | Low-level varint/record primitives for the binary formats |
| `auth/` | `UbisoftAuth` facade + auth-shortcut context, session monitor, shortcut registry ops |
| `library/` | `UbisoftLibrary` facade + fetch, data-loading, install detection, game-building, manifest, steam filter, free-to-play feed |
| `installer/` | `UbisoftInstaller` + manual-UI install driver, installer-download cache, uninstall/update pipelines |
| `prefix/` | `UbisoftPrefixManager` — auth/template/per-game prefix lifecycle and MachineGuid identity |
| `session/` | `UbisoftSession` — credential validation, best-source selection, propagation to all prefixes |

Ubisoft-specific code **outside** the package:

| Path | Responsibility |
| ---- | -------------- |
| `launcher/proton/handlers/ubisoft.py` | Launch handler: resolve `uplay://` id from id-map, find `upc.exe`, run deeplink |
| `launcher/proton/language_setup/ubisoft.py` | Patch game language in `system.reg` at launch |
| `rpc/mixins/auth_shortcuts.py` | `get_ubisoft_auth_shortcut_context` RPC |
| `src/utils/ubisoftShortcutLaunch.ts` | Frontend: `launchUbisoftInstallViaShortcut` / `launchUbisoftAuthViaShortcut` |
| `src/lib/steam-bridge/temp-shortcut.ts` | Shared `AddShortcut`/`RemoveShortcut` temp-shortcut helpers |
| `src/components/play/DownloadingButtons.tsx`, `src/components/downloads/DownloadProgressRow.tsx` | `download_phase === "manual"` UI (indeterminate + Cancel) |

---

## 3. The Three-Prefix Model

Ubisoft credentials are DPAPI-encrypted and bound to a Wine `MachineGuid`, so prefixes
that must share a sign-in must share an identity. Three prefix roles exist under
`prefixes_dir` (`~/.local/share/unifideck/prefixes/ubisoft/`):

| Prefix | Dir | Purpose |
| ------ | --- | ------- |
| **Auth** | `.upc-auth/` | The single prefix the user actually signs into. UPC writes `ConnectSecureStorage.dat` + `user.dat` here. |
| **Template** | `.template/` | A UPC-installed baseline cloned to create new game prefixes; carries the shared `MachineGuid`. |
| **Per-game** | `{space_id}/` (or a recorded path on SD/custom storage) | One prefix per installed game; receives propagated credentials. |

The prefix manager keeps the template's identity in sync with the auth prefix, and
**refuses to copy credentials into a prefix whose `MachineGuid` differs** (the copy would
be undecryptable). `repair_prefix(space_id)` re-aligns a diverged game prefix.

---

## 4. Authentication Flow

Auth is **not** a REST/2FA call. It launches the real UPC client and watches for the
credential files it writes.

1. **Frontend** calls `launchUbisoftAuthViaShortcut()`.
2. **Backend** `UbisoftStore.start_auth()` ensures the `.upc-auth` prefix exists
   (installing UPC into it on first run) and starts the credential session monitor.
3. **Frontend** requests the auth-shortcut context via the
   `get_ubisoft_auth_shortcut_context` RPC. The backend ensures a persistent
   "Ubisoft Connect" shortcut exists in `shortcuts.vdf` (store id `ubisoft:upc-auth`) and
   returns `{ appid, launcher_path, launch_wait_ms, launch_options, ... }`.
4. **Frontend** calls `SteamClient.Apps.RunGame(appid)` with
   `UNIFIDECK_UBISOFT_ACTION=auth` (+ `UNIFIDECK_UBISOFT_PREFIX_NAME=.upc-auth`). UPC
   launches in the auth prefix and shows its login UI.
   - **First-session caveat:** Steam only loads `shortcuts.vdf` into its in-memory app
     store at startup, so a freshly written persistent shortcut may not be runnable mid
     session. The launch path falls back to a **temp shortcut** (`AddShortcut`, helpers in
     `src/lib/steam-bridge/temp-shortcut.ts`) that is cleaned up after the session.
5. **User** signs in inside UPC; UPC writes `ConnectSecureStorage.dat` + `user.dat`.
6. **Session monitor** detects the new credential files, captures them, and the session
   layer **propagates** them to the template and every game prefix (subject to the
   `MachineGuid` guard).
7. **Frontend** observes `STORE_AUTH_COMPLETE`; the library auth-gate now passes.

`logout()` deletes the auth prefix (background task). `is_available()` reports auth by
checking the auth prefix for valid credentials — this is also the **library auth gate**
(see §5).

---

## 5. Library Flow

`UbisoftStore.get_library()` is auth-gated (returns empty when not signed in — a
deliberate anti-phantom-shortcut safeguard) and builds the library from local data:

1. **Detect installed games** — scan the install bases for `.unifideck-id` markers
   (`{space_id, install_id, executable, install_path}`), with heuristic fallbacks.
2. **Load UPC catalogs** — locate and parse, off the event loop:
   - `configuration/configurations` → `GameConfig` records (`install_id`, `launch_id`,
     `space_id`, `name`, `executable`, …) via `parser.parse_configurations`.
   - `ownership/{userId}` → the set of owned `install_id`s via `parser.parse_ownership`.
3. **Resolve Connect IDs** — read `space_id → ubisoftConnectGameId` pairs from UPC's
   localStorage leveldb (`leveldb_ids`); these are the most reliable `uplay://` IDs.
   The scan is object-scoped: only values inside the same JSON record pair, and an ID
   claimed by two games is discarded (issue #436). A conflict sweep over the persisted
   map runs first, repairing entries written before that fix.
4. **Build games** — cross-reference configs × ownership × installed state, dedup DLC
   against parents using the cached community game-ID database.
5. **Filter / supplement** — optional Steam-linked dedup (`filter_steam_linked`), manifest
   overrides, and an optional public free-to-play CDN feed
   (`enable_free_to_play_feed`, source `https://static3.cdn.ubi.com/orbit/uplay_launcher_14_0/.../free_games/latest.txt`).

> There is **no GraphQL / web API call** in this path. (A vestigial code comment mentions
> GraphQL; the network library fetch was removed.)

### Binary catalog format (parser.py)

UPC stores its catalog as protobuf-like, length-prefixed binary records inside the prefix.
Records are framed by a `0x0A` marker + varint size; fields use `0x08`/`0x10`/`0x1A`
markers, with an embedded YAML blob carrying `name` / `space_id` / `executable`. The
scanner is **index-based and always advances ≥1 byte**, so a malformed region can never
wedge it into an infinite loop — a regression that previously hung library sync at
"Ubisoft 5/5".

---

## 6. Install Flow

UPC has no silent-install mode, so the backend drives a *manual* install and reports an
indeterminate phase rather than a fake percentage.

1. `UbisoftStore.install_game(game_id, …)` bootstraps the per-game prefix
   (`prefix/manager.bootstrap_game_prefix`): ensure auth → ensure template → clone/repair
   the game prefix and inject credentials.
2. Ensure the UPC installer binary is present (downloaded once, cached under
   `ubisoft_installer_cache/`).
3. Signal the frontend (`on_ready`) to `RunGame` the shortcut with
   `UNIFIDECK_UBISOFT_ACTION=install`, so UPC opens with the game's `uplay://install/{id}`
   in the game prefix.
4. **Manual-UI driver** (`installer/manual_ui.py`) snapshots install dirs, then polls for a
   new game directory that *stabilises* across consecutive checks (size/file-count
   heuristics) — it does not wait for UPC to exit.
5. On completion: write the `.unifideck-id` marker, update `ubisoft_id_map.json` (including
   the recorded `prefix_path` for SD/custom storage), inject the install into the prefix
   registry, and SIGTERM UPC.

**Download-queue UX:** the install stays in the normal download queue but with
`download_phase = "manual"`. The frontend shows an indeterminate "Installing in Ubisoft
Connect" state (no fake %/speed) and a **Cancel** that SIGTERMs the UPC process
(`DownloadingButtons.tsx`, `DownloadProgressRow.tsx`).

---

## 7. Launch Flow

`launcher/proton/handlers/ubisoft.py :: ubisoft_launch(plan)`:

1. **Resolve the prefix path** from `ubisoft_id_map.json` for the `space_id`, preferring a
   recorded `prefix_path` (SD/custom installs) over the default
   `prefixes_dir/{space_id}`.
2. **Resolve the launch id**, preferring `ubisoftconnect_game_id` (from the leveldb cache),
   then `launch_id`, then `install_id`.
3. **Apply language setup** — patch
   `[Software\WOW6432Node\Ubisoft\Launcher\Installs\{install_id}]` `"Language"` in
   `system.reg`.
4. **Seed the session** — `ubisoft_handoff.seed_before_launch()` copies the newest known
   auth state into this prefix before UPC reads it.
5. **Launch** — if `upc.exe` is found and a launch id resolved, run
   `upc.exe uplay://launch/{launch_id}/0` under Proton/umu in the per-game prefix.
6. **Capture the session back** — in a `finally`, `ubisoft_handoff.capture_after_exit()`
   waits (bounded) for UPC to exit, captures the token it rotated, and fans it out to the
   other prefixes.

### Why the launch path moves credentials at all

Ubisoft retires the previous refresh token whenever a UPC instance signs in, so with a
prefix per game the session is a **baton, not a snapshot**: only the prefix that ran last
holds a live token. Steps 4 and 6 are what pass it. Step 6 duplicates the backend's
`GAME_STOPPED` subscriber (`stores/ubisoft/store.py`) on purpose — that event is bridged
from the frontend, and when it does not arrive the token is stranded in the game prefix.
Both are mtime-gated and idempotent, so whichever runs second is a no-op. All of it is
best-effort: a launch never fails because a credential could not be moved.

Freshness is decided by **mtime, never file size** — see §12.

---

## 8. ID Map (`ubisoft_id_map.json`)

Location: `~/.local/share/unifideck/ubisoft_id_map.json`. Per-`space_id` entry:

```json
{
  "<space_id-guid>": {
    "install_id": "12345",
    "launch_id": "12345",
    "ubisoftconnect_game_id": "12345",   // preferred for uplay://
    "ubisoftconnect_game_id_source": "leveldb", // who wrote it; gates overwrites
    "prefix_path": "/abs/path/to/prefix" // set when installed to SD/custom storage
  }
}
```

The map is the bridge between the frontend's `space_id`, UPC's numeric IDs, and the
launcher (which reads it directly). `resolve_install_id` / `resolve_launch_id` prefer
`ubisoftconnect_game_id` when present.

Every write of that ID goes through `UbisoftIdMap.set_connect_id`, which admits it only
from an equal-or-higher-confidence source than the one already recorded. The ladder,
weakest first: `name_db` < `configurations` < `manifest` < `leveldb` < `registry` <
`manual`. An entry with no source key predates the #436 fix and is treated as untrusted.
So a force sync cannot overwrite the registry ID of an installed game, and a hand-edited
`ubisoft_id_map.json` survives every sync.

---

## 9. File & Path Conventions

```
~/.local/share/unifideck/
├── ubisoft_id_map.json                 # space_id ↔ ids ↔ prefix path
├── ubisoft_installer_cache/            # cached UPC installer .exe
└── prefixes/ubisoft/
    ├── .upc-auth/                       # auth prefix (user signs in here)
    ├── .template/                       # cloning baseline
    └── {space_id}/                      # per-game prefix (default location)

~/Games/Ubisoft/<game>/                  # default install base (configurable)
└── .unifideck-id                        # install marker {space_id, install_id, executable, install_path}
/run/media/mmcblk0p1/Games/Ubisoft/      # default SD-card install base
```

UPC paths inside a prefix (under `drive_c/users/steamuser/AppData/Local/Ubisoft Game Launcher/`):
`cache/configuration/configurations`, `cache/ownership/{userId}`, and the credential files
`ConnectSecureStorage.dat` / `user.dat`.

---

## 10. Configuration (`stores.ubisoft.*`)

Defined in `config.py` (`UbisoftConfig`). Notable keys and defaults:

| Key | Default | Meaning |
| --- | ------- | ------- |
| `id_map_file` | `~/.local/share/unifideck/ubisoft_id_map.json` | ID-map location |
| `prefixes_dir` | `~/.local/share/unifideck/prefixes/ubisoft` | Wine prefix base |
| `default_install_base` | `~/Games/Ubisoft` | Default game install dir |
| `sdcard_install_base` | `/run/media/mmcblk0p1/Games/Ubisoft` | SD-card install dir |
| `auth_prefix_name` | `.upc-auth` | Auth prefix folder |
| `template_prefix_name` | `.template` | Template prefix folder |
| `auth_shortcut_store_id` | `ubisoft:upc-auth` | Steam shortcut store id for auth |
| `filter_steam_linked` | `false` | Hide Ubisoft games already in the Steam library |
| `enable_free_to_play_feed` | `false` | Supplement library with the public F2P CDN feed |

---

## 11. Frontend Integration

| Concern | Where |
| ------- | ----- |
| Launch UPC for **install** | `launchUbisoftInstallViaShortcut()` (`src/utils/ubisoftShortcutLaunch.ts`) — resolves compat context, waits for shortcut registration, `RunGame`, restores the user's Proton tool & launch options afterward |
| Launch UPC for **auth** | `launchUbisoftAuthViaShortcut()` (same file) — passes `UNIFIDECK_UBISOFT_ACTION=auth` |
| Temp-shortcut fallback | `src/lib/steam-bridge/temp-shortcut.ts` (`AddShortcut`/`RemoveShortcut`) for the first session before Steam reloads `shortcuts.vdf` |
| Install confirm modal | Ubisoft-specific copy in the game-action interceptor; i18n keys `confirmModals.ubisoftInstall{Title,Description,Confirm}` |
| Manual install progress | `DownloadProgressRow.tsx` / `DownloadingButtons.tsx` on `download_phase === "manual"` |

---

## 12. Notable Constraints & Gotchas

- **Auth gate on library** — no credentials ⇒ empty library, by design (prevents phantom
  shortcuts for a signed-out store; see the reconcile sweep in the sync pipeline).
- **MachineGuid identity** — the session layer refuses to copy credentials between
  prefixes whose `MachineGuid` differs. This is a conservative identity check, **not** a
  decryption requirement, and the difference matters: Wine's `CryptProtectData`
  (`dlls/crypt32/protectdata.c`) derives its key from the Windows user name, a hardcoded
  constant and a salt stored inside the blob — no master keys on disk, and the machine
  GUID is not an input. A vault decrypts fine in any prefix whose Windows user is the
  same `steamuser`. Earlier revisions of this document claimed otherwise, and that claim
  sent a bug reporter chasing GUIDs for a session problem that had nothing to do with
  them (GH #435).
- **Session freshness is mtime, never size** — Ubisoft rotates the refresh token on every
  sign-in and a rotated `ConnectSecureStorage.dat` is routinely a few hundred bytes
  *smaller* than the one before it. The capture and propagate guards used to read
  "smaller" as "signed out", which made the largest vault ever written win permanently:
  the auth prefix froze on a retired token, and signing into one game signed the user out
  of the others (GH #435). Signed-in is now decided by shape — a vault with a `user.dat`
  beside it — and ordering by mtime. The pristine `.template` has no `user.dat`, which is
  what makes the test work.
- **The handoff is serialised** — the backend fan-out and the launcher's seed/capture
  both rewrite the same files from different processes. `session/lock.py` holds an
  `fcntl.flock` over `<data_dir>/ubisoft-session.lock` around every write. Acquisition is
  bounded and callers proceed unlocked on timeout; a launch must not stall on it.
- **Connect-ID preference** — `uplay://launch` wants the `ubisoftConnectGameId`; the
  leveldb extraction in `leveldb_ids` is the authoritative source for a not-installed
  game, with `launch_id` / `install_id` as fallbacks. For an installed game the prefix's
  own `Launcher\Installs\<id>` registry key outranks it: the leveldb cache is shared
  across every game, and #436 showed it can hand one game its neighbour's ID.
- **No fake progress** — UPC drives the real download; the manual phase is honestly
  indeterminate, and Cancel maps to a SIGTERM of UPC.
- **Recorded prefix path** — installs to SD/custom storage record an absolute
  `prefix_path` in the ID map so the launcher finds the prefix regardless of mount layout.

---

_The original design rationale and the (now-divergent) REST/GraphQL design live in the v1
spec, retained locally at `docs/archive/ubisoft-store-spec-v1.md` (kept out of version
control)._
