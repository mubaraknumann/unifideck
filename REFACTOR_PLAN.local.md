# Unifideck Refactor Plan (LOCAL)

This file is intentionally **not committed**. Keep it local-only.

- Branch: refactor/mega-cleanup
- Draft PR: https://github.com/xXJSONDeruloXx/unifideck/pull/4

## Non‑negotiables

1. Do not rename/remove Decky RPC callables or change their args/return shapes.
2. Do not change cache formats under ~/.local/share/unifideck/ unless versioned+migrated.
3. Prefer move code, not logic.
4. Small commits.

## Phase 0 — Guardrails

- CI workflow green
- Starter pytest tests for pure modules

## Phase 1 — Backend helper extraction (shrink main.py) ✅ COMPLETE

**Final:** main.py reduced from 4171 → 3224 lines (947 lines / 22.7% reduction)

- ~~extract cache helpers (game sizes, steam_appid, etc.)~~
- ~~extract metadata cache functions (steam_metadata, rawg_metadata)~~
- ~~extract Steam appinfo.vdf utilities~~
- ~~extract shortcuts registry~~
- ~~extract metadata utilities (description sanitization, format conversion)~~
- ~~extract self-contained functions (inject_single_game_to_appinfo)~~
- ~~remove compat cache duplication (source of truth: backend/compat)~~
- ~~simplify backend imports (remove redundant BACKEND_AVAILABLE checks)~~
- ~~extract artwork utilities~~
- ~~extract deck compat & RAWG metadata utilities~~

Gates after each commit:

- python3 -m compileall -q . -x "py_modules|node_modules|out|dist|bin"
- npm run build
- CI green

## Phase 2 — Backend structure (controllers/services/adapters) ✅ PARTIAL COMPLETE

**Status:** Core services extracted, Plugin is now primarily a wiring layer

**Completed:**

- ✅ Extract InstallService (game installation/uninstallation logic)
- ✅ Extract ArtworkService (grid/hero/logo fetching and caching)
- ✅ Extract MetadataService (ProtonDB, RAWG, Deck verification)
- ✅ Consolidate path constants (SETTINGS_PATH, LEGENDARY_CONFIG_DIR, etc.)

**Results:**

- main.py reduced from 3224 → 3001 lines (223 lines / 6.9% reduction)
- Created backend/services/ with 3 service classes (632 total lines)
- Plugin class now delegates to services for:
  - Install/uninstall operations (InstallService)
  - Artwork fetching and management (ArtworkService)
  - Metadata fetching and compat lookups (MetadataService)

**Remaining work:**

- Extract SyncService (library sync operations) - COMPLEX, needs careful extraction
- Reorganize store adapters (Epic/GOG/Amazon) for consistency
- Further simplify Plugin to pure coordination layer

**Target:** main.py ~1500-2000 lines (requires SyncService extraction)

Gates maintained after each commit:

- python3 -m compileall -q . -x "py_modules|node_modules|out|dist|bin"
- npm run build
- All gates passing ✅

## Phase 3 — Frontend split (src/index.tsx) 🚧 IN PROGRESS

**Goal:** Separate concerns in the React frontend

**Completed:**

- ✅ Extract Steam patching logic → src/patching/GameDetailsPatching.tsx (~192 lines)
- ✅ Extract data hooks → src/hooks/useGameInfo.ts & useDownloadState.ts (~206 lines)

**Results:**

- index.tsx reduced from 1834 → 1522 lines (312 lines / 17.0% reduction)
- Created src/patching/ with 1 module (192 lines)
- Created src/hooks/ with 2 custom hooks (206 lines)
- Improved code reusability and testability

**Remaining work:**

- Extract InstallButton component (use extracted hooks)
- Extract remaining settings components
- Further UI/state separation

**Target:** Cleaner component structure, easier to maintain and extend

## Phase 4 — Stabilize

- more tests, type tightening, error normalization
