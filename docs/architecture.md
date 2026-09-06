# Unifideck — Architecture & Build Process

> **Version:** 0.7.4 · **Last re-synced:** 2026-08-24 (see `docs/architecture-audit.md`)

---

## 1. Overview

Unifideck is a [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin that provides a unified game library for Steam Deck, integrating Epic Games, GOG, Amazon Games, Ubisoft Connect, Battle.net, and Microsoft PC Game Pass directly into Steam's interface.

The 0.7 restructure replaced the legacy monolithic layout with a strict **layered Python package architecture** driven by an EventBus, a dependency-injection service container, and a typed RPC surface.

---

## 2. Repository Layout

```
unifideck-decky/
├── main.py                  # Decky entry point — Plugin class + RPC composition
├── plugin.json              # Decky plugin manifest (name, version, api_version)
├── package.json             # JS manifest + remote_binary bundling spec
├── requirements.txt         # Vendored Python deps (pip install --target py_modules/)
├── defaults/
│   ├── config.json          # User config schema + default values
│   └── backend/             # Backend-specific default data files
├── bin/                     # Native binaries & shell wrappers (no .py scripts)
├── py_modules/              # All Python runtime code (vendored + unifideck/)
│   └── unifideck/           # The plugin's own layered package
├── src/                     # TypeScript/React frontend
├── assets/                  # Plugin artwork and icons
├── tests/                   # pytest test suite (mirrors py_modules/unifideck/)
├── scripts/                 # Dev/CI helper scripts (not bundled)
├── docs/                    # Documentation (this file)
└── build-plugin.sh          # Local build script (this document's subject)
```

---

## 3. The Layered Backend Architecture

The diagram below is the authoritative layer model. Do not restate a layer
count in prose — it has drifted between "five" and "six" across this repo.
Imports flow **downward only** — no layer may import from a layer above it.
The machine-enforced invariants are in §9 (`.importlinter`).

```
┌─────────────────────────────────────────────────┐
│  Layer 6 — RPC (main.py + rpc/mixins/)          │  ← Decky JS bridge
├─────────────────────────────────────────────────┤
│  Layer 5 — Services (services/)                 │  ← Infrastructure services
├─────────────────────────────────────────────────┤
│  Layer 4 — Stores (stores/)                     │  ← 7 store connectors
├─────────────────────────────────────────────────┤
│  Layer 3 — StoreBase (stores/shared/)           │  ← Abstract store contract
├─────────────────────────────────────────────────┤
│  Layer 2 — Core (core/)                         │  ← CacheManager, SyncService…
├─────────────────────────────────────────────────┤
│  Layer 1 — Types (core/types/)                  │  ← Game, Result, Events (pure data)
└─────────────────────────────────────────────────┘
```

### Layer 1 — `core/types/`

Pure data island. No side-effects, no I/O. Safe to import from any layer.

| File         | Contents                                      |
| ------------ | --------------------------------------------- |
| `domain.py`  | `Game`, `StoreId`, `InstallState` dataclasses |
| `events.py`  | All typed `EventBus` event payloads           |
| `results.py` | `Result[T]` envelope (success/error/code)     |

### Layer 2 — `core/`

Infrastructure primitives. No store or service knowledge.

| Module/Package                  | Purpose                                   |
| ------------------------------- | ----------------------------------------- |
| `cache_manager.py`              | Namespace-keyed in-memory + disk cache    |
| `manifest.py`                   | Plugin installation manifest reader       |
| `metrics_collector.py`          | Latency/counter telemetry                 |
| `exe_finder.py`                 | Heuristic executable discovery            |
| `paths.py`                      | Canonical path resolution                 |
| `store_urls.py`                 | Per-store storefront/search URL builders  |
| `cross_source_dedupe.py`        | Drops a title owned on two stores at once |
| `safe_delete.py`                | Guarded delete used by every sweep        |
| `cleanup_sweeps.py`             | The blocking sweeps behind "delete all data" |
| `marker_sweep.py`               | Install-dir ownership via `.unifideck*` markers |
| `stale_installs.py`             | Detects install records with no files left |
| `compat_bridge.py`              | Bridges our prefixes into `compatdata/` so Protontricks can see them; owns the signed/unsigned AppID pair |
| `compat_tool_bridge.py`         | Resolves a compat-tool id to a Proton path |
| `steam_appid_map.py`            | The one read of the shortcut → real-Steam-AppID cache that returns "an AppID or 0" |
| `store_capabilities.py`         | Per-store capability sets — the single source of truth behind the `get_store_infos` flags |
| `io/async_file_ops.py`          | Async file read/write/remove              |
| `io/safe_file_op.py`            | Atomic write with rollback                |
| `binaries/binary_resolver.py`   | Resolves `bin/` tool paths                |
| `binaries/binary_signatures.py` | SHA-256 verification for bundled binaries |
| `binaries/cli_timeouts.py`      | Per-tool subprocess timeout config        |

The library sync is split across an orchestrator and the mixins it composes,
enumerated below, all in `core/` rather than in `rpc/`. That placement is
deliberate but it does blur the "rpc is thin" boundary, since these hold logic
an RPC mixin calls into:

| Module                   | Purpose                                        |
| ------------------------ | ---------------------------------------------- |
| `sync_service.py`        | Cross-store library sync orchestration         |
| `sync_run_mixin.py`      | Drives one sync run across the stores          |
| `sync_cache_mixin.py`    | Reads and writes the library cache             |
| `sync_queries_mixin.py`  | Per-game lookups against a synced library      |
| `sync_results_mixin.py`  | Assembles the per-store result envelope        |
| `sync_finalize_mixin.py` | Post-sync reconcile and cleanup                |
| `sync_progress.py`       | Phase/percentage model behind the sync bar     |
| `sync_availability.py`   | Whether a store can be synced right now        |
| `sync_generation.py`     | Run ids; skips a repeat post-sync chain        |

### Layer 3 — `stores/shared/`

`StoreBase` ABC — defines the ten abstract methods every store must implement:
`is_available`, `start_auth`, `complete_auth`, `logout`, `get_library`,
`install_game`, `uninstall_game`, `update_game`, `check_for_updates`,
`get_game_size`. There is no `launch()` on the store contract — launching is
owned by `launcher/dispatcher.py`.

### Layer 4 — `stores/`

Seven store connector sub-packages. Each is self-contained with its own auth, library, install, and update logic.

| Package             | Store                   | Backend                              |
| ------------------- | ----------------------- | ------------------------------------ |
| `stores/epic/`      | Epic Games Store        | `bin/legendary`                      |
| `stores/gog/`       | GOG                     | `bin/gogdl` + `bin/comet`            |
| `stores/amazon/`    | Amazon Games            | `bin/nile`                           |
| `stores/ubisoft/`   | Ubisoft Connect         | UPC client in a per-game Wine prefix |
| `stores/battlenet/` | Battle.net              | Battle.net client in a Wine prefix   |
| `stores/microsoft/` | PC Game Pass / xCloud   | Edge browser + CDP                   |
| `stores/gamevault/` | GameVault (self-hosted) | The user's own server over HTTP, or a local folder of archives |

### Layer 5 — `services/`

Infrastructure services that subscribe to the EventBus and own cross-cutting concerns.

| Service package                    | Responsibility                                |
| ---------------------------------- | --------------------------------------------- |
| `services/download/`               | Download queue, progress tracking, worker     |
| `services/playtime/`               | Session recording, DB persistence             |
| `services/cloud_save/`             | Save sync — upload/download/conflict          |
| `services/shortcut/`               | Steam VDF shortcut create/delete/update       |
| `services/artwork/`                | SteamGridDB artwork fetching                  |
| `services/launcher/`               | Game launch orchestration, circuit breaker    |
| `services/security/`               | Token store, bruteforce protection, audit log |
| `services/microsoft_subscription/` | Game Pass entitlement probing                 |
| `services/launch_history/`         | Per-game launch timestamps                    |
| `services/achievements/`           | Achievement fetch + last-session summary      |
| `services/compatibility/`          | ProtonDB + Valve per-device ratings (Deck / Machine / SteamOS) |
| `services/playtime_sync/`          | Pushes playtime back to GOG/Epic              |
| `services/support_bundle/`         | Capture Logs: collects the diagnostic zip     |
| `services/updater/`                | Plugin self-update check and download         |
| `services/bootstrap/`              | DI container, service constructor, teardown   |
| `metadata_service.py`              | Metacritic + UnifiDB metadata aggregation     |
| `account_service.py`               | Multi-account lifecycle                       |
| `proton_service.py`                | Proton version resolution                     |
| `post_sync_reconcile.py`           | Boot-time repair of interrupted post-sync data |

### Layer 6 — `rpc/mixins/` + `main.py`

The `Plugin` class in `main.py` is composed from the RPC mixin classes enumerated in the table below, which is the same set listed in `main.py` `class Plugin(...)`; two more (`CleanupRPCMixin`, `_CleanupFinalizeMixin`) arrive transitively through `SyncRPCMixin`. No count is given here on purpose: a hardcoded figure has gone stale three times through the 0.7.x series. `main.py` and `rpc/mixins/__init__.py.__all__` are the source of truth, and their agreement is machine-checked by `scripts/validate_architecture.py` (check 1), which also fails if a numeric mixin count reappears in prose. The `@auto_wrap_rpc_methods` decorator rewrites every public coroutine to return a typed `Result[T]` envelope, keeping the frontend contract stable across backend refactors.

| Mixin                      | Surface (representative)                                                        |
| -------------------------- | ------------------------------------------------------------------------------- |
| `StoreRPCMixin`            | `check_store_status`, `get_store_infos`, `store_auth`, `connect_gamevault`, `connect_gamevault_local`, `clear_store_auths` |
| `SyncRPCMixin`             | `sync_libraries`, `force_sync_libraries`, `get_game_info`, `get_sync_progress`   |
| `DownloadRPCMixin`         | `install_game`, `uninstall_game`, `update_game`, `cancel_download`, `get_download_queue`, `get_available_updates` |
| `StorageRPCMixin`          | `get_storage_locations`, `get_browseable_devices`, `set_custom_install_path`    |
| `LaunchRPCMixin`           | `notify_game_launched`, `notify_game_stopped`, `get_launch_failures`             |
| `AuthShortcutsRPCMixin`    | `get_<store>_auth_shortcut_context`, `get_compat_tool_for_game`                  |
| `EdgeRPCMixin`             | `install_edge`                                                                  |
| `ExecutableRPCMixin`       | `list_game_executables`, `set_game_executable`, `reset_game_executable`          |
| `LibraryFacetsRPCMixin`    | `get_overview_enrichment`                                                       |
| `PlaytimeRPCMixin`         | `get_playtime`                                                                  |
| `ObservabilityRPCMixin`    | `subscribe_replay`, `get_launcher_toasts`, `capture_logs`                        |
| `ActionRPCMixin`           | `dispatch_unifideck_action` (URI dispatch)                                      |
| `AccountRPCMixin`          | `check_account_switch`, `migrate_account_data`                                  |
| `AchievementsRPCMixin`     | `get_game_achievements`, `get_last_session_achievements`                         |
| `CloudSaveRPCMixin`        | `get_cloud_save_status`, `cloud_save_pull`, `cloud_save_push`, `set_game_save_path` |
| `UIRPCMixin`               | `get_game_metadata_display`, `get_language_preference`, `set_language_preference`, `get_device_type` |
| `UpdaterRPCMixin`          | `check_plugin_update`, `get_available_versions`, `log_update_event`              |

---

## 4. Support Packages

These sit alongside the layered stack and can be imported by any layer.

| Package          | Description                                                                                                |
| ---------------- | ---------------------------------------------------------------------------------------------------------- |
| `accounts/`      | Account-switch detection + data migration (backs `AccountRPCMixin`)                                         |
| `auth/`          | OAuth browser monitor + multi-store auth orchestrator + Edge browser shims                                 |
| `cdp/`           | Chrome DevTools Protocol injection utilities                                                               |
| `compatibility/` | Proton/Wine prefix management and helper wrappers                                                          |
| `event_bus/`     | The message backbone. Broken out below, because half of it is not on the emit path                          |
| `config/`        | Config manager, JSON schema validator, i18n schema, startup validation                                     |
| `bootstrap/`     | DI wiring: `boot_plugin`, `unload_plugin`, `build_eventbus_pipeline`, cache registry                       |
| `security/`      | Ephemeral credential store, secure I/O, device fingerprint, audit emission, redaction                      |
| `metadata/`      | Metacritic scraper, UnifiDB API client                                                                     |
| `steam/`         | Steam library path discovery, VDF shortcuts, SteamGridDB, owned games                                      |
| `utils/`         | Shared path helpers, locale utilities, config helpers                                                      |
| `launcher/`      | Game launcher dispatcher, Proton infrastructure, language setup, cloud save trigger, CDP flows, game fixes |
| `actions/`       | `dispatch.py` — `unifideck://` URI handler; `unifideck_uri.py` — URI parser                                |
| `rpc/`           | `auto_wrap_rpc_methods` decorator + `rpc/mixins/` composition                                               |

### `event_bus/` in detail

Split out because the package contains two layers and only one of them runs.
`EventBus.emit` writes straight to the replay buffer; it never feeds
`PriorityDispatcher`. So the whole priority/coalescing/back-pressure layer and
the handler watchdog are unreachable in production, which a Capture Logs
bundle confirms empirically (`bus_health.dispatcher` reports
`emitted_total: 0` on a session where events demonstrably flowed, and
`bus_health.watchdog` is `{}`). Their fate is register items 4e and 4g in
`architecture-audit.md`: either make the dispatcher the real emit path, or
delete the layer. Do not build on it before that is decided.

| Module                      | On the emit path? | Purpose                                        |
| --------------------------- | ----------------- | ---------------------------------------------- |
| `event_bus.py`              | yes               | `EventBus`: subscribe, emit, 60s handler timeout |
| `bus_pipeline.py`           | yes               | Assembles the bus and its collaborators at boot |
| `event_replay.py`           | yes               | Per-event replay ring, so a late subscriber catches up |
| `event_bus_devex.py`        | yes               | `subscribe`/`auto_wire` decorators, schema extraction |
| `event_bus_extensions.py`   | yes               | Typed payloads, schema registry, dead-letter queue |
| `event_bus_reliability.py`  | yes               | Retry and dead-letter handling                 |
| `priority_dispatcher.py`    | **no**            | Priority queue + coalescing; never fed         |
| `event_priority.py`         | **no**            | `EventPriority` enum + the coalescing key map  |
| `event_bus_scaling.py`      | **no**            | Back-pressure helpers for the dispatcher       |

`supervision/` holds the two handler wrappers: `metrics_handler.py` (live) and
`watchdog_handler.py` (nothing registers with it, see 4g).

---

## 5. `bin/` Directory

Contains **only** compiled binaries and shell wrappers. All old `bin/*.py` helper scripts have been absorbed into `py_modules/unifideck/launcher/` and the relevant service packages.

| File                            | Size    | Role                                                                                     |
| ------------------------------- | ------- | ---------------------------------------------------------------------------------------- |
| `legendary`                     | ~4.5 MB | Epic Games Store CLI (upstream) — Python zipapp                                          |
| `gogdl`                         | ~1.5 MB | GOG download manager (upstream, Heroic) — Python zipapp                                  |
| `nile`                          | ~10 MB  | Amazon Games CLI (upstream, imLinguin)                                                   |
| `comet`                         | —       | GOG online services / Galaxy stub (upstream, imLinguin)                                  |
| `winetricks`                    | ~820 KB | Wine component installer (shell script)                                                  |
| `unifideck-launcher`            | shell   | Entry-point wrapper — bootstraps `py_modules/` path and calls `launcher.dispatcher.main` |
| `unifideck-launcher.py`         | Python  | Python source for the launcher (companion to the shell wrapper)                          |
| `unifideck-runner`              | shell   | Minimal wrapper for Proton runs                                                          |
| `EpicGamesLauncher.exe`         | 150 KB  | Stub wrapper used by Legendary for Epic auth                                             |
| `vcruntime_fix.reg`             | 1 KB    | Windows registry patch for VC runtime in Wine prefix                                     |
| `stubs/GalaxyCommunication.exe` | binary  | GOG Galaxy overlay stub (copied into Wine prefix by the GOG store)                       |
| `umu/`                          | dir     | `umu-run` runtime bundle (upstream project)                                              |

---

## 6. `py_modules/` — Vendored Python Dependencies

Installed via `pip install --target py_modules/ -r requirements.txt`.

**Active runtime deps:**

| Package        | Purpose                                      |
| -------------- | -------------------------------------------- |
| `aiohttp`      | Async HTTP client (used across ~10 modules)  |
| `websockets`   | WebSocket support (CDP, auth browser)        |
| `vdf`          | Valve Data Format parser (Steam shortcuts)   |
| `certifi`      | TLS CA bundle                                |
| `aiofiles`     | Async file I/O helpers                       |
| `filelock`     | Cross-process file locking                   |
| `legendary`    | Legendary Python library (Epic auth helpers) |
| `steamgrid`    | SteamGridDB Python bindings                  |
| `cryptography` | Token encryption in `security/`              |

**Removed in v0.7 restructure** (no longer bundled):

- `requests`, `urllib3`, `idna`, `charset_normalizer` — replaced by `aiohttp`
- `pip/`, `py_modules/bin/` — packaging artefacts, not runtime deps

---

## 7. Frontend (`src/`)

TypeScript/React frontend compiled to `dist/index.js` by Rollup.

The frontend communicates with the backend exclusively via Decky's RPC bridge — it calls the public methods of the `Plugin` class (which are the RPC mixin surfaces) and receives typed `Result[T]` envelopes.

Key architectural landmarks post-restructure:

- **`src/index.tsx`** — reduced from 2 409 LOC to ~166 LOC (plugin registration only)
- **`src/lib/steam-bridge/`** — new Steam interaction abstraction layer
- **`src/views/`** — QuickAccessPanel, AppDetailsPatch
- **`src/components/`** — decomposed into `play/`, `info/`, `modals/`, `settings/`, `shared/`, `downloads/`
- **`src/hooks/`** — `useSteamLibrary`, `usePlaySection`, `useGameActions`
- **`src/types/`** — typed sub-package (`store.ts`, `steam.ts`, `downloads.ts`, `playtime.ts`, `syncProgress.ts`)

---

## 8. Build Process

### Prerequisites

- `pnpm` (for frontend build)
- `curl` (binary downloads)
- Docker or Podman (for Decky CLI builds) **or** nothing (local fallback)
- `zip`, `unzip`

### Script usage

```bash
./build-plugin.sh [dev|prod] [install|quick-install] [push]

# Examples:
./build-plugin.sh                # dev build, no install
./build-plugin.sh prod           # production build
./build-plugin.sh install        # dev build + auto-install to Decky
./build-plugin.sh quick-install  # no build; rsync source into the live install
./build-plugin.sh push           # dev build + publish it as a Dev-* prerelease
./build-plugin.sh install push   # all three
```

The mode is optional and defaults to `dev`, so `install`, `quick-install` and
`push` all stand on their own; spelling out `dev` first is equivalent. If a mode
is named it must come first.

Every argument is validated; an unrecognised one exits 1 with the usage banner
rather than being silently ignored. `-h` / `--help` prints the same banner.

`push` is the only argument that contacts GitHub, and it is dev-only. Without it
a build stays entirely local. With it, the zip is published as a **new**
`Dev-<date>-<time>-<sha>` prerelease and the previous `Dev-*` release (and its
tag ref) is deleted — that is what testers pick up through the in-plugin
updater. Production releases do not use this path; they go through the draft
release flow instead.

### Build flow

```
build-plugin.sh
    │
    ├─ prebuild_binaries()
    │       Download/verify: legendary, gogdl, nile, comet, winetricks
    │       Source of truth: package.json "remote_binary" array + SHA-256 hashes
    │
    ├─ check_requirements()
    │       Ensure requirements.txt exists (fallback: copy from requirements.in)
    │
    ├─ sync_version()
    │       Read version from plugin.json (no auto-increment)
    │
    ├─ check_decky_cli()  ──→  [CLI available?]
    │       Yes → check_container_engine()
    │               Docker/Podman found → build_with_cli()
    │               No container      → build_local()
    │       No  → build_local()
    │
    └─ [install?] → install_plugin()
```

### `build_with_cli` (Docker/Podman path)

1. Cleans `dist/` (handles root-owned files from previous container builds)
2. Creates a clean staging directory containing:
   `py_modules/`, `bin/`, `defaults/`, `src/`, `assets/`, `main.py`, `plugin.json`, `package.json`, `pnpm-lock.yaml`, `tsconfig.json`, `rollup.config.mjs`, `requirements.txt`, `LICENSE`, `README.md`
3. Runs `decky plugin build` inside the container — compiles frontend + packages everything
4. Renames `Unifideck.zip` → `unifideck.[dev|prod].vN.zip`

### `build_local` (Steam Deck / no container)

1. Runs `pnpm run build` to compile frontend → `dist/`
2. Copies staged files (same set as above) + `dist/` into a temp `Unifideck/` directory
3. Runs **critical file verification** — 80+ path assertions covering all layers
4. Sets executable bits on `bin/`
5. Zips into `out/unifideck.[dev|prod].vN.zip`

### `install_plugin`

1. Stops `plugin_loader` systemd service
2. Removes existing `~/homebrew/plugins/Unifideck/`
3. Extracts new zip into `~/homebrew/plugins/`
4. Sets `deck:deck` ownership + `755` permissions
5. Starts `plugin_loader`

### Output naming

| Mode        | Pattern                                 | Example                     |
| ----------- | --------------------------------------- | --------------------------- |
| Production  | `unifideck.prod.vX.Y.Z.zip`             | `unifideck.prod.v0.7.0.zip` |
| Development | `unifideck.dev.vN.zip` (auto-increment) | `unifideck.dev.v12.zip`     |

---

## 9. Architectural Constraints (`.importlinter`)

Two import invariants are machine-enforced:

| Contract        | Rule                                                                                     |
| --------------- | ---------------------------------------------------------------------------------------- |
| `rpc-is-leaf`   | Nothing inside `unifideck.*` may import `unifideck.rpc` — only `main.py` may             |
| `types-is-leaf` | `core.types` may not import from `event_bus`, `services`, `stores`, `launcher`, or `rpc` |

Run `lint-imports` (via `pyproject.toml`) to verify these invariants in CI.

---

## 10. Binary Version Manifest

All remote binaries are declared in `package.json` under `"remote_binary"`. The build script derives download URLs and validation from this manifest. **Keep these two in sync.**

| Binary       | Version  | URL                                             |
| ------------ | -------- | ----------------------------------------------- |
| `legendary`  | 0.20.43  | `github.com/Heroic-Games-Launcher/legendary`    |
| `gogdl`      | v1.2.2   | `github.com/Heroic-Games-Launcher/heroic-gogdl` |
| `nile`       | v1.1.2   | `github.com/imLinguin/nile`                     |
| `comet`      | v0.3.2   | `github.com/imLinguin/comet`                    |
| `winetricks` | 20260125 | `github.com/Winetricks/winetricks`              |

`umu` is the exception: it is committed to the repo (`bin/umu/umu/umu-run`) rather than downloaded, so it has no `remote_binary` entry. Its version is recorded in `bin/umu/VERSION` (currently **1.4.4**) and reported in support bundles. Do not ship umu &lt;= 1.4.1: those versions fetch the Steam Linux Runtime from `repo.steampowered.com/<variant>/images/latest-public-beta[/VERSION.txt]`, which the repo now answers with HTTP 403. umu's *update* path tolerates that and keeps an existing runtime working, but its *install* path fails, so any Deck without a cached runtime can never obtain one. 1.4.3+ reads `images/latest-public-beta.txt` and fetches from the numbered directory it names, which serves normally.

`nile` is deliberately held at v1.1.2. v1.2.0 migrates auth into an encrypted store and **deletes** `~/.config/nile/user.json` on first run — the file `AmazonStore._check_nile_authenticated` reads to decide the store is available. Bumping it without migrating that check silently empties the Amazon library for users who are still perfectly authenticated.

**Packaging note:** `legendary` (>= 0.20.40) and `gogdl` (>= 1.2.2) ship as Python **zipapps**, not PyInstaller ELF binaries. They require a `python3` on `PATH` (via `#!/usr/bin/env python3`) and a writable `HOME` — on first run they extract native modules to `~/.cache/legendary/vendored` and `~/.cache/heroic_gogdl/vendored`. Unlike a frozen ELF they also honour `PYTHONPATH`/`PYTHONHOME` and the dynamic-loader variables, which is why every store-CLI spawn goes through `core/binaries/cli_env.clean_cli_env()`.
