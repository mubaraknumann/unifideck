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

## Phase 1 — Backend helper extraction (shrink main.py)

**Progress:** main.py reduced from 4171 → 3433 lines (738 lines / 17.7% reduction)

- ~~extract cache helpers (game sizes, steam_appid, etc.)~~
- ~~extract metadata cache functions (steam_metadata, rawg_metadata)~~
- ~~extract Steam appinfo.vdf utilities~~
- ~~extract shortcuts registry~~
- ~~extract metadata utilities (description sanitization, format conversion)~~
- ~~extract self-contained functions (inject_single_game_to_appinfo)~~
- ~~remove compat cache duplication (source of truth: backend/compat)~~
- ~~simplify backend imports (remove redundant BACKEND_AVAILABLE checks)~~
- Continue looking for more extractable helpers/utilities

Gates after each commit:

- python3 -m compileall -q . -x "py_modules|node_modules|out|dist|bin"
- npm run build
- CI green

## Phase 2 — Backend structure (controllers/services/adapters)

- main.py becomes wiring

## Phase 3 — Frontend split (src/index.tsx)

- isolate Steam patching vs UI vs data hooks

## Phase 4 — Stabilize

- more tests, type tightening, error normalization
