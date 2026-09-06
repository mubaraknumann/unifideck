# Engineering Roadmap — workflow & infrastructure backlog

Written at the v0.7.0 handoff (2026-07-02, commit c64dbe0) after an audit of the repo, CI, open issues, and the unifiDB pipeline. These are **workflow/infrastructure** improvements, not feature requests. Pick from the High-impact / Low-effort cell first. Each item: Problem → Evidence → Sketch → Files.

## Priority matrix

| | Low effort | Medium effort | High effort |
|---|---|---|---|
| **High impact** | #3 issue templates+labels, #6 coverage ratchet | #1 unifiDB automation, #2 release automation | #4 frontend tests, #10 cross-store test harness |
| **Medium impact** | #7 binary-URL dedup, #8 stale-docs guard | #5 RPC contract check, #9 metadata cache TTL | |

---

## 1. unifiDB update automation — High impact / Medium effort

**Problem**: The game catalog (matching quality for every store) refreshes only when someone manually runs `download_igdb_cache.py` + `split_igdb_cache.py` and pushes. Catalog staleness shows up as user-facing "no metadata found" issues.
**Evidence**: unifiDB last regenerated 2026-02-03; open issues repeatedly report missing metadata/matches (e.g. "No metadata found on other games", Ubisoft title-not-found reports).
**Sketch**: Monthly GitHub Actions workflow in the unifiDB repo: download (with its existing ≥100k sanity floor) → split → commit → tag `igdb-YYYY-MM-DD`. Failure = workflow failure notification, no partial publish. Keep `@main` layout stable (the bucket scheme is a live API for shipped plugins — see `.claude/skills/unifideck-release/unifidb-pipeline.md`).
**Files**: `unifiDB/.github/workflows/` (new), `unifiDB/download_igdb_cache.py`, secrets `IGDB_CLIENT_ID`/`IGDB_CLIENT_SECRET`.

## 2. Release automation — High impact / Medium effort

**Problem**: Version bump, staging→main merge, tag, changelog, GitHub Release, and artifact upload are all manual and undocumented outside the release skill.
**Evidence**: Tag zoo (`Release-0.6.1`, `Release1`, `Release_0.2.1`) shows the manual process drifting; `build-plugin.yml` builds artifacts but never publishes releases.
**Sketch**: Workflow on `Release-*` tag push: `./build-plugin.sh prod` → GitHub Release with the ZIP + changelog generated from conventional commits since the previous tag. Optionally a `workflow_dispatch` that also bumps `package.json`/`plugin.json` and opens the staging→main PR.
**Files**: `.github/workflows/release.yml` (new), `build-plugin.sh`, `package.json`, `plugin.json`.

## 3. Issue templates + auto-labeling — High impact / Low effort

**Problem**: All open issues are unlabeled free-text; triage (which store? bug or request? which device?) happens by reading each one. The Trello sync carries no signal either.
**Evidence**: 30+ open issues, zero labels (checked 2026-07-02). Recurring themes are clearly store-clustered (Epic launch/auth, Ubisoft sync, GOG saves) but invisible in the list view.
**Sketch**: `.github/ISSUE_TEMPLATE/` with a bug form (store dropdown, device/distro, plugin version, log excerpt) + feature form; a labeler action maps the store dropdown to labels; extend `scripts/sync_to_trello.py` to carry labels onto cards.
**Files**: `.github/ISSUE_TEMPLATE/` (new), `.github/workflows/add-issue-to-trello.yml`, `scripts/sync_to_trello.py`.

## 4. Frontend test coverage — High impact / High effort

**Problem**: Vitest is configured but only a couple of test files exist (`src/api/event-bus-client.test.ts`, `src/lib/steam-bridge/collection-manager.test.ts`, `src/lib/library-facets.test.ts`). UI-patching and event-classification regressions are only caught on-device.
**Evidence**: The event-replay/stale-state bug class recurred several times; each fix carries frontend logic (`STALE_ON_RELOAD_EVENTS`, `IMPERATIVE_EVENTS`) with no regression tests beyond the client itself.
**Sketch**: Target pure-logic modules first (library filters/facets, steam-bridge helpers, event classification, gog-language matching); add `pnpm run test` as a hard gate in `quality.yml`; grow toward hook tests with a mocked `@decky/api`.
**Files**: `src/**/*.test.ts`, `.github/workflows/quality.yml`, `vitest.config.ts`.

## 5. RPC contract validation (TS ↔ Python) — Medium impact / Medium effort

**Problem**: `src/api/rpc-routes.ts` route names must match Python mixin method names by convention only; a rename on either side fails at runtime with a generic RPC error.
**Evidence**: The event-schema equivalent (`scripts/validate_event_schemas.py`) already exists precisely because the same drift class bit events; RPC has no counterpart.
**Sketch**: Script that ASTs the mixins for public coroutine names, parses `rpc-routes.ts`, and diffs both ways; wire into `quality.yml` next to the event-schema step.
**Files**: `scripts/validate_rpc_routes.py` (new), `src/api/rpc-routes.ts`, `py_modules/unifideck/rpc/mixins/`, `.github/workflows/quality.yml`.

## 6. Coverage ratchet (`fail_under`) — High impact / Low effort

**Problem**: CI measures and uploads coverage but enforces nothing — coverage can silently fall.
**Evidence**: `tests.yml` has `--cov` + Codecov upload; no `fail_under` anywhere; the tiered targets (80% core, 62% tier-4, 40% overall) live only in comments.
**Sketch**: Set `--cov-fail-under` at the CURRENT total (ratchet, not aspiration) and raise it opportunistically; optionally per-package gates for `core/` via a coverage config.
**Files**: `.github/workflows/tests.yml`, `pytest.ini` or `pyproject.toml [tool.coverage]`.

## 7. Binary URL de-duplication — Medium impact / Low effort

**Problem**: Bundled-CLI URLs/versions exist in BOTH `package.json` (`remote_binary`, with SHA-256) and `build-plugin.sh` (`prebuild_binaries()`); bumps must touch both.
**Evidence**: The two lists already require a warning in the release skill; drift = building with a stale binary while declaring a new one.
**Sketch**: Make `build-plugin.sh` parse `package.json` (python3 one-liner, no jq dependency) and delete its hardcoded URLs; verify SHA-256 from the manifest while at it.
**Files**: `build-plugin.sh`, `package.json`.

## 8. Stale-docs guard — Medium impact / Low effort

**Problem**: Agent-facing docs rot invisibly and then actively mislead. This has already happened once.
**Evidence**: The pre-rewrite `.github/copilot-instructions.md` described `defaults/backend/`, RAWG, and `build-plugin_old.sh` — an architecture dead since 0.7 — with no signal it was stale. <!-- mixin-count-ok: historical citation; the 2026-08 audit §2.1 defect this item exists to prevent --> `main.py`'s own docstring claimed "eleven mixins" while the class composed 20; that specific instance is closed (`scripts/validate_architecture.py` check 5 now rejects any mixin count written in prose), but it is the shape of the problem this item is about.
**Sketch**: (a) `Last verified:` headers now exist on CLAUDE.md + all skills — bump them with related PRs (see CONTRIBUTING.md). (b) Small CI script that greps `.claude/skills/**/*.md` + `CLAUDE.md` for repo-relative path tokens and fails on nonexistent ones. (c) ~~Delete the dead root artifacts~~ — **struck 2026-08-26**: all of them are untracked *and* gitignored, so they reach no contributor and deleting them would change the repo not at all while irrecoverably destroying the maintainer's local backups. Audit register item 42.
**Files**: `scripts/check_agent_docs.py` (new), `.github/workflows/quality.yml`.

## 9. Metadata cache TTL / schema versioning — Medium impact / Medium effort

**Problem**: unifiDB/Metacritic/PCGW metadata caches have no TTL or schema version; stale entries survive both catalog refreshes and plugin updates.
**Evidence**: "Missing metadata" reports that resolve after manual cache deletion; jsDelivr itself has a ~12h edge TTL the plugin-side cache should complement, not fight.
**Sketch**: Add `schema_version` + per-source TTL to the cache entries (CacheManager already supports TTL); invalidate on plugin-version change for the metadata sources only.
**Files**: `py_modules/unifideck/core/cache_manager.py`, `py_modules/unifideck/metadata/unifidb.py`, `services/metadata_service.py`.

## 10. Cross-store parametrized test harness — High impact / High effort

**Problem**: Every store connector implements the same contract (auth-gated library fetch, install-status overlay, exe resolution) with store-specific code; regressions are found store-by-store, in production.
**Evidence**: The GOG install-overlay regression (installed games flipping to "not installed" after every sync) was a contract violation that a parametrized suite would have caught for every store at once. Store bugs dominate the issue tracker.
**Sketch**: `tests/stores/test_store_contract.py` parametrized over all registered stores: `get_library` overlays installs and sets `exe_path`; empty-on-auth-failure never reports an empty owned library; ids are stable across syncs. Feed with recorded fixtures, not live APIs.
**Files**: `tests/stores/` (new), `py_modules/unifideck/stores/shared/`.

---

## Horizon (unscoped)

- **Companion Steam desktop app** — a separate initiative that was in early research at handoff time (CDP-based Steam integration outside Decky). Ask the maintainer before investing.
- Frontend RPC types generated from Python signatures (extends #5).
- Per-store API sandbox tests where stores offer staging environments.
