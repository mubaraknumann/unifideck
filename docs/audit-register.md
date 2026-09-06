# Audit remediation register

> **This file is the tracker.** The narrative it came from is
> `docs/architecture-audit.md` (historical, do not edit). The device-validation
> steps every `VALIDATING` row depends on are in `docs/device-validation.md`.
>
> Last updated: 2026-08-28 · Source review: 2026-08-24 against v0.7.5
>
> **Progress:** 15 CLOSED · 38 VALIDATING (fixed, awaiting the Deck) · 7 OPEN
> · 1 DECLINED. Seven gate blind spots were the durable half; see the gate
> table. The check-13 convergence backlog is drained (16 groups → 1, a
> deliberate keep).
>
> **The 38 `VALIDATING` rows are the real state of this programme.** Nothing
> is closed until it runs on a Deck; every one of those rows names its step in
> `device-validation.md`, and the rows that legitimately need no step say so
> in that file's "Needs no device validation" table.

## States

| State | Meaning |
|---|---|
| `OPEN` | Not started, or started and not finished. |
| `VALIDATING` | Code landed and gates pass, but **not yet confirmed on a Deck**. Not closed. See `device-validation.md`. |
| `CLOSED` | Fixed and confirmed — on device where the change is user-visible, by gate/test where it is not. |
| `DECLINED` | Deliberately not doing it. The reason is recorded in the tree, not only here. |
| `BLOCKED` | Cannot proceed until a **decision** is made — not until other code lands. The blocking question is named in the row. |

A row moves to `CLOSED` only when every device-validation step it names has
passed. Ticking a box without that is the failure this file exists to stop.

## ID rules

IDs are permanent and never reused. The 2026-08-24 register issued `26` three
times and `27` twice; those were resolved on 2026-08-26 as below, and the old
numbers must not be cited again.

| Old | Topic | Now |
|---|---|---|
| 26 (§3.3 pass) | legacy Ubisoft bootstrap markers | **43** |
| 26 (§3.1 pass) | `StoreInfo` write-only descriptor | **26** |
| 26 (§3.2 pass) | `check_version` / `min_version` | **44** |
| 27 (§3.1 pass) | `vulture_whitelist` group comments | **27** |
| 27 (§3.2 pass) | GOG-only progress phases | **45** |

`24` and `24a` were never parent and child — `24` is the vulture blind spot,
`24a` is the launch-options wipe. Both keep their ids.

---

## P0 — silent correctness

| ID | Title | State | Notes |
|---|---|---|---|
| 1 | Retire the phantom `GAME_INSTALLED` event | VALIDATING | DV-A1 |
| 2 | `MetricsCollector` timers/gauges wired | CLOSED | Fixed by `8b62be5` before the register was worked; verified both sides. |
| 3 | `TOAST_NOTIFICATION` retired onto `LAUNCHER_STAGE` | VALIDATING | DV-B1…B5 |
| 4 | `DOWNLOAD_*` single-emitter | VALIDATING | DV-C1…C8 |
| 4a | Circuit breaker is user-invisible and unresettable | VALIDATING | Built 2026-08-26 (G2). New `useCircuitState` subscribes `circuit_state_changed` — written from the **corrected** payload contract, since the enum docstring had documented four keys that were never sent — with `get_launch_failures` as the mount fallback, because the event only fires on a *change*. Badge + Force-launch + Reset render in `PlayMeta`, gated on the breaker actually being open. Three routes added; **three `# no-frontend-caller:` exemptions removed** (5 → 2) and the event's `# unwired:` exemption removed (5 → 4). **Zero new strings** — all of `library.circuitBreaker.*` already existed in 16 locales; 3 keys drained from the dead-key baseline. Depends on **46**, without which the badge would show failures that never clear. **Validate:** DV-M. |
| 4b | No frontend renders a toast action | VALIDATING | **Unblocked and built 2026-08-26** once 37 was decided — before that a generic renderer would have had nothing to render. New `resolveToastAction` shared by both listeners; Decky toasts take an `onClick`, not a button, so the toast is the affordance and the label sits in the subtext. **Three producers of one field had drifted into three shapes**: `_emit_save_conflict` sent `{verb,args}`, `cloud_failure` sent `{i18n_label_key,target_url}`, and `launcherToasts.tsx` redeclared the type a third time. All converged on `{verb,args,i18n_label_key}`; a Steam deep link is the `open-url` verb so one shape covers both. **The trap:** `retry-sync` now has two producers with opposite intent — a real conflict (with snapshots, needs the pick modal) and a transient failure (nothing to choose). Branching on the verb alone would open a pick modal with two empty sides on every dropped Wi-Fi, so both renderers discriminate on the snapshots via `isConflictAction`. Unknown verbs fail closed. Three action labels became reachable for the first time; the dead `openSaveFolder`/`showLogs` pair was deleted (their modals do not exist) and the baseline shrank. **Validate:** DV-P. |
| 4c | `Result.error_code` never set on launch failures | VALIDATING | Fixed 2026-08-26 at all three sites, so `CIRCUIT_BREAKER_OPEN` (9) stops being a dead branch and the classification is no longer discarded. **The second half resolved the other way:** `ExitCode.user_message_key()` was deleted, not wired — eight of the nine `toasts.launcher.*` keys it returned **were never written into any locale**, so wiring it would have shown users raw key names. That is the inverse of §1.1.2 and nothing could catch it, which is why `check_orphan_keys` gained **check 4** (backend-named keys must have a string; all 48 real `i18n_key=` literals resolve). Specific per-failure messages are a deliberate 8×16-translation feature, filed with **28**/**49**. **Validate:** DV-M. |
| 4d | `check_hardcoded_strings` positional-key false positive | CLOSED | Call site now names the argument. |
| 4e | Event coalescing configured but never runs | OPEN | **Decision 2026-08-26: wire it** (plan G1d). `COALESCE_KEY[DOWNLOAD_PROGRESS]` names a kwarg no emitter sends. |
| 4f | No `download_cancelled` counter | VALIDATING | Done 2026-08-26. Incremented inside `_on_download_cancelled`, **not** as a `counter_events` row — that event already has a `@subscribe` handler and a row wires it twice (`test_every_handler_is_wired_exactly_once` catches it). Guard: `test_a_cancelled_download_is_counted`, verified against a planted violation. DV-C6 covers it. |
| 4g | `HandlerWatchdog` inert | VALIDATING | Wired 2026-08-26 (G1a + G1b). **Not by editing 20 call sites** — services get a bus, not the pipeline, so threading it by hand meant a new parameter on every service with 20 chances to miss one. `pipeline_factory` attaches the watchdog to the **bus** and `auto_wire` falls back to it: one assignment, no call-site churn. Boot order asserted (layer 2 pipeline before layer 5 services), since a service built earlier would silently miss registration — the same bug one layer up. `EventBus._invoke` now runs handlers through `watchdog.invoke`, with a thunk so **sync** handlers still go to a thread (`invoke` awaits directly and would have raised on every one) and a fallback so a stub watchdog can never block delivery. A quarantine skip logs at WARNING, not ERROR — it is a deliberate skip and the other handlers still run. Corrected `priority_dispatcher`'s docstring, which claimed "the bus uses the watchdog internally" from the start. `emit()` hit the 80-line cap and was **split**, not allowlisted. 9 tests, verified against a planted removal of the fallback. Needs DV-N1. |
| 4h | Probe quarantine calls a method that does not exist | CLOSED | Fixed 2026-08-26 — call site and `hasattr` string both now `quarantine_preemptive`. Signature already matched. Still unreachable until 4i, but no longer wrong; gate-verified (mypy over 565 files). |
| 4i | Runtime-probe pipeline unbuilt end to end | OPEN | **Decision: build** (G1e), but scoped after 4e/4g prove the layer carries traffic. |
| 4j | `LaunchLogsService.export` callerless | VALIDATING | Done 2026-08-26: `export` deleted, and **both** docstrings that asserted an `export_launch_logs` RPC corrected (`launch_logs.py` and `service_defs.py`) — the RPC was deleted in the §1.2 pass and Capture Logs already collects the same `launches/*.log` files. `read`/`get_launch_logs` kept but recorded as unreachable pending 4b/37. Corrected a third false comment found alongside: `service_defs.py` called the plugin's `LaunchHistoryService` "read-only by convention" — item 46 made it the **only** writer that can clear a tripped breaker. **Validate:** SW5 (the bundle must contain `launches/*.log`). |
| §1.3 | Event-bus mismatches | VALIDATING | DV-D1…D8 |
| §1.2 | Dead RPC methods (29 of 102) | CLOSED | User-validated on device 2026-08-25. |
| **46** | **Circuit breaker never resets on a successful launch** | VALIDATING | **P0, found and fixed 2026-08-26.** Reads `exit_code`/`elapsed_seconds` now — the names the emitter sends and `CANONICAL_SCHEMA` declares. Elapsed is measured by a new `_on_game_launched` monotonic stamp rather than by changing any payload, so no emitter, RPC or frontend call changed. New `tests/unit/test_launch_history_game_stopped_contract.py` (7 tests, the service had **none** before); 4 of them fail against the original `rc`/`elapsed` code — verified by planting it. Needs DV-L1. |

## P1 — consolidation

| ID | Title | State | Notes |
|---|---|---|---|
| 5 | Promote `merge_install_status` + `_rebuild_auth_after_injection` | VALIDATING | DV-E1…E12 |
| 6 | Unify CLI progress parsers; lift GOG's stall watchdog into shared | VALIDATING | DV-F1…F13 |
| 7 | Link the wrapper/CLI tables | VALIDATING | DV-G1…G9 (+G10, destructive-adjacent) |
| 8 | Migrate Ubisoft onto `shared/prefix_clone` | VALIDATING | DV-H1…H14 |
| 9 | Extract encrypted-token persistence | VALIDATING | DV-I4, DV-I5 |
| 10 | Delete `StoreBase._run_cli` | CLOSED | Deleted; zero callers confirmed. |
| 11 | Microsoft stubs return `not_supported` | VALIDATING | DV-J5, DV-J6 |
| 12 | `resolve_proton_path` collision + security package split | VALIDATING | DV-I1, DV-I2, DV-I3 |
| 19 | Encrypt Epic/Amazon credentials at rest? | OPEN | Mode (0600) is done; encryption is a separate decision. Wraps every legendary/nile invocation. |
| 20 | Three copies of `appid_candidates` | VALIDATING | Done 2026-08-26. Canonical in `core/compat_bridge.py` beside `to_unsigned` — **not** in `rpc/`, which is a leaf package two of the copies could not import. Routed 4 call sites (mypy found a fourth the audit never listed, `_library_facets.py`). Both it and `to_unsigned` are now pinned in `SHARED_HELPERS`. **Validate:** DV-W2. |
| 26 | `StoreInfo` is a mostly write-only descriptor | VALIDATING | Done 2026-08-26 with **31**. New `core/store_capabilities.py` is the single source of truth; `get_store_infos` injects four capability flags beside `available`/`client_runs_in_prefix`. `src/types/api.ts` now mirrors the wire shape (it declared `icon` and `auth_status`, **neither ever sent**, and omitted four that were). **Found while doing it:** `supports_cloud_saves` was a `StoreInfo` field only Battle.net declared — as `False` — so GOG and Epic, the only stores with cloud saves, both advertised none. Field deleted; a re-added literal raises `TypeError`. 10 parity tests pin each set against the code that implements it (registered strategies, defined methods, exposed RPCs). **Validate:** DV-R. |
| 43 | Upgrade legacy Ubisoft bootstrap markers in place | OPEN | Precondition: DV-H11 must pass first. |
| 44 | `BinaryResolver.check_version` / `CLITool.min_version` unused | VALIDATING | Decided 2026-08-26: **deleted**, as one unit — `check_version`, `min_version`, `version_flag`, the three per-store `version_flag=` literals, the `_version_timeout` knob, its `binary_resolver.version_check_timeout_seconds` config key (defaults + schema + `RUNTIME_REQUIRED_KEYS`) and both vulture-whitelist rows. The only apparent call site was inside the class's own `Usage::` docstring. Deleted rather than built because SHA-256 pinning in `package.json` is the stronger guarantee, and the tree's one real version constraint is a **maximum** (nile pinned at 1.1.2) that `min_version` could not express. **Validate:** DV-W1. |

## P2 — documentation and tooling

| ID | Title | State | Notes |
|---|---|---|---|
| 13 | Layer model stated only in the `architecture.md` diagram | CLOSED | Check 6 enforces; found two sites the audit missed. |
| 14 | Mixin count stated in exactly two machine-checked places | CLOSED | Check 5 enforces. |
| 15 | Six stores everywhere | CLOSED | Check 7 verifies rather than bans. |
| 16 | `StoreBase` contract documented correctly | CLOSED | Re-verified name by name. |
| 17 | Phantom "handler group" docstrings; undocumented subpackages | CLOSED | Check 8 enforces; caught 25 omissions on first run. |
| 18 | Version reconciliation | CLOSED | Closed by deleting `__version__`, not bumping it. |
| 20a | Move `cleanup_sweeps.py` out of the RPC leaf | CLOSED | `git mv`, no logic change, both import contracts KEPT. |
| 21 | Strip the `OP-XX` markers | VALIDATING | Done 2026-08-26. **Zero `OP-` references remain** in `py_modules/`, `main.py`, `src/`, `scripts/` or `tests/`; `docs/archive/` is untouched by design. 151 files: 147 banner lines (`OP-09 | <path>` — a stale plan id plus a path restating the file's own) deleted outright, 71 inline parentheticals stripped in four measured shapes (` (OP-x)`, `, OP-x)`, ` from OP-x)`, `(owned by OP-x)`), 2 handled by hand. **Not a `sed` job, as the audit warned** — only 3 of 220 were `#` comments; the rest were inside docstrings, where a line-delete would have corrupted the prose. **Validate:** none needed. |
| 22 | Re-verify stale skill `Last verified:` stamps | VALIDATING | **Done 2026-08-28.** The durable half first: **check 14** of `validate_architecture.py` asserts every `.claude/skills/**/*.md` carries a `Last verified: YYYY-MM-DD` stamp. It is a **local** gate that skips cleanly when the directory is absent — `.claude/` is gitignored, so CI cannot observe or fix the condition, and failing there would break every run. It checks *existence, not accuracy*: no machine can tell whether prose still describes the code, and existence was the half that was missing. 14 tests, verified against a planted stamp removal. Then all seven stale files were re-derived, and reading them found five real errors rather than just old dates: `stores.md` cited `check_version` as unwired under **the pre-renumber id 26** when it had been deleted entirely (item 44); `launcher.md` said `state.wrappers` was "unpopulated" and that item 23b "deletes it" in the future tense, when it is gone and `game_args` is now live; `unifidb-pipeline.md`'s pipeline diagram **omitted `enrich_save_locations.py`**, which runs *between* download and split, so following the diagram would ship a catalog with no save-path data; `unifideck-bug-triage/SKILL.md` named `scripts/tracker.py` immediately after the bugtrack repo's path, where no such file exists (it is in *this* repo); and **`CLAUDE.md` listed four of the six skills** — `unifideck-drift-guard` and `unifideck-bug-triage` were missing from the one file loaded into every session. Two files (`unifideck-dev-loop/SKILL.md`, `unifideck-release/SKILL.md`) were audit C-4's case exactly: content current, stamp stale, the release one even carrying a measurement *newer* than its own stamp. Each new stamp records what was re-derived and what was not, so a stamp is never a blanket claim. **Validate:** none needed — prose and a local gate; check 14 and its tests are the guard. |
| 23 | Wire the launch-options parser | VALIDATING | DV-K4 is the one that matters. DV-K7/K8/K10/K11 are unsatisfiable — see 23a/23b. |
| 23a | Wire `state.game_args` | VALIDATING | Done 2026-08-26, **after fixing the precondition at its source**. `extractUserParams` preserved the user's `mangohud`/`gamemoderun` into the tail, so populating `game_args` would have handed them to the *game*. It now keeps only `KEY=value` assignments — a bare word after the game key was never a wrapper, because Steam applies wrappers pre-exec (§2.9, measured). With that and 23b, a bare token in the tail is honestly a game argument, which is what Steam delivers it as. 3 tests incl. the no-options regression guard. Needs DV-Q. |
| 23b | Delete `ParsedOptions.wrappers` / `RuntimeState.wrappers` | VALIDATING | Done 2026-08-26. Both fields deleted, 6 argv builders now start empty, and the parser **drops** tokens before a `%command%` instead of re-homing them into `game_args` — re-homing them would be the §2.9 hazard, which my first simplification reintroduced and the tests caught. 12 tests updated to assert the field's **absence** rather than its emptiness, so it cannot quietly return. Needs DV-Q. |
| 24 | Vulture cannot see an unimported module | VALIDATING | Done 2026-08-26. New **check 12** in `validate_architecture.py` (hard) + **8 dead modules deleted**: `launcher/diagnostics/{telemetry,save_folder_inspector}.py`, `launcher/proton/fixes/auth_args_stripper.py`, `launcher/signals.py`, `security/audit_decorators.py` (a duplicate definition of the live `audit_emitter.audit_auth_flow`), `services/cloud_save/{fs_ops,paths}.py`, `steam/steamgriddb/match.py`. Verified: a planted orphan fails check 12 and vulture at 80 stays silent on it. 10 tests. Two opt-outs, `# entry-point:` and `# unimported:`, deliberately distinct. **Validate:** SW1–SW5. |
| 24a | `_update_existing_shortcut` wiped launch options | CLOSED | Severity corrected on device: a settled library takes the reclaim path, so the wipe was real code on an unreached path. |
| 25 | Steam-exported env dropped by the container escape | OPEN | Low priority; the working route is documented and verified. |
| 27 | `vulture_whitelist.py` group comments false per-member | VALIDATING | Rewritten 2026-08-26 with per-name reasons. Every entry was tested **empirically** — delete the line, re-run vulture at confidence 60, see whether a report appears — rather than by reading. Result: **22 of 51 entries suppressed nothing even at 60** and were deleted; the surviving 29 each earn their place, and the suppressed set is byte-identical before and after. Two survivors are labelled as deletion candidates rather than dressed up as live. **The headline finding is new:** at the gate's own `min_confidence = 80` the whitelist suppresses **nothing at all** — 0 hits with it and 0 without — so the entire file is inert against the check CI runs. That is why it rotted, and it is item **24**'s blind spot from the other side. **Validate:** none needed. |
| 42 | Delete the dead root artifacts | **DECLINED — premise wrong** | Re-derived 2026-08-26: all six are **untracked and gitignored** (`build-plugin_old*.sh`, `main.py.backup` 244 KB, `.gitignore.backup`, `task.md`, root `test_ubisoft_launch.py`). Roadmap #8 asks to delete them as clutter that misleads contributors — but nothing reaches a contributor: they are not in the repo and never were. Deleting them changes the repo not at all, is **not recoverable from git**, and destroys what are plainly the maintainer's own local backups. That is absolute rule 2 (never delete user data; ask first). Nothing to do here; roadmap #8's part (c) should be struck. |

## Store behaviour and convergence

| ID | Title | State | Notes |
|---|---|---|---|
| 28 | Stall message reaches the user in English | VALIDATING | **Done 2026-08-28.** `errors.download.stalled` written in all 16 locales by hand (translations are manual here, not machine-generated) and a classifier rule added in `src/lib/download-errors.ts`. **The rule was wrong on the first attempt and the tests caught it**: `friendlyDownloadError` matches `CODE_KEYS` against the text *before the first colon*, so `/^stalled: no output for /` could never fire — the extracted code is the bare word `stalled`. That contract is now stated in the comment beside the rule, since nothing else in the file says it. Both the seconds and the phase word vary and neither is actionable to a user, so one key carries both cases; two test rows pin `downloading` and `finalizing`. It fires for all three CLI stores, because the stall watchdog moved into the shared drain loop. **Validate:** DV-F7 already covers it — the step forces a stall and now also asserts the message is localized. |
| 29 | Build the Battle.net `game_accounts` producer | OPEN | Every F2P and subscription title is invisible. **Measure DV-J4 before building.** DV-J4 is now readable from a log rather than a debugger: `library._log_ownership_inputs` prints licences/game_accounts/flags and fragments/programs/titles/granted on every sync (added 2026-09-01 for GitHub #447). |
| **51** | **`AccountFacts.flags` is a second always-empty input** | OPEN | New 2026-09-01, found triaging GitHub #447 — the same shape as **29** but not previously recorded. `library.read_account_facts` builds `AccountFacts` with `licence_ids` and `game_account_programs` only, so `flags` is permanently `frozenset()` and `ownership/rules._match_flag` can never fire, whatever a real Blizzard PUB rule says. Decide it explicitly: find the source or delete the matcher — a third correct-machinery-fed-nothing input is exactly what made **29** invisible for a release. |
| **52** | **A partially written PUB cache is treated as authoritative** | OPEN | New 2026-09-01, GitHub #447 (reporter saw a one-game Battle.net library). `library.read_library` returns `None` — "unreadable", which protects the user's shortcuts from the stale sweep — only when `program_configurations` is **completely** empty. A first sync racing the client's cache write passes that guard with a handful of fragments and persists a library far smaller than the account. A real prefix measures ~254 fragments / 38 with program rules. **The threshold must be measured, not invented**: take it from a reporter log now that item 29's diagnostics print both counts. |
| 30 | Stale-sweep invariant not machine-checked | VALIDATING | Done 2026-08-26 with the **nominal** form, not a check: `valid_stores` is a `NewType("SweepableStores", frozenset[str])` that only `_sweepable_stores` builds. Verified against a planted `reconcile(games, valid_stores=set(registry.store_ids()))` — the exact line that caused §3.5 finding B — which mypy now rejects. Also deleted a comment in `reconcile_phases.py` that still **recommended** the widening ("a caller can widen this to every registered store"). 2 guard tests. **Validate:** none needed. |
| 31 | `_ACHIEVEMENT_STORES` has an unlinked frontend twin | VALIDATING | Done 2026-08-26 with **26**. Four TS lists deleted — both copies of `CLOUD_SAVE_STORES` (`useCloudSaveStatus.ts` and `PlayMeta.tsx`, the second admitting in a comment that it mirrored the first), the inline `gog||epic` achievements condition, and `LANGUAGE_STORES`' gate half. New `useStoreCapability` / `storeHasCapability` read the payload and **fail closed**. `LANGUAGE_ROUTE` survives because it maps store→route, which the payload does not carry. **Validate:** DV-R. |
| 32 | Ubisoft update path has no trigger; GOG DLC has no route | OPEN | Decide each as one unit: build the missing half or delete it. |
| 33 | No pre-install size or space guard for the wrapper stores | DECLINED | **Maintainer decision, 2026-08-28: the vendor client owns the install, so this is theirs to guard.** The finding is accurate as stated — wrapper stores *do* reach `validate_path`, but always with `required_bytes=None` (`_resolve_required_bytes` returns early for `uses_manual_download_phase`), so only the static 1 GB floor applies and a 90 GB Ubisoft install can begin with 1 GB free. It is declined because the size is not ours to know: UPC and the Battle.net Agent choose what to download and report their own space errors, and a floor we invented would either block small titles or wave through large ones. Revisit only if a wrapper store starts exposing a reliable pre-install size — that is the fact that changes the arithmetic, not user complaints about the current behaviour. |
| 45 | Progress phases exist for GOG only | VALIDATING | Resolved 2026-08-26, and the framing was half wrong. Epic/Amazon needed **nothing**: `worker.py` already stamps `download_phase="preparing"` centrally for every store, and the UI localizes from the phase alone. What was real was six **decorative** `phase_message` producers in GOG (`"Extracting…"`, `"Verifying… 12.3%"`) restating the localized label in hardcoded English — deleted. **Validate:** DV-U2. |
| **49** | **A measured wrapper-store wait explanation is computed and discarded** | OPEN | New, found while doing 45 — and the reason 45 was *not* a blanket delete. `battlenet/install_watch.status_message` reads the Agent's log and reports "queued behind the self-update", which is the known `battlenet-agent-one-operation-queued` failure the user otherwise sees as a permanent "Queued". It reaches the queue item as `phase_message` and **no frontend renders it** (`DownloadProgressRow.tsx` ignores the field on purpose because it is English). Wiring it is an i18n decision, same shape as **28**. |
| **48** | **`get_installed_path` duplicated across three stores** | VALIDATING | Done 2026-08-26. New `stores/shared/installed_path.install_path_from_record`, consumed by GOG, Ubisoft and Amazon (`key="path"` carries nile's field name). **Only the guard is shared, not the fetch** — §3.2's lesson; Epic and Battle.net keep their own resolvers because their sources differ in kind. The guard is where §3.4 found the live blank-path defect. 14 tests incl. the empty-string and JSON-array cases; check 11 verified to reject a re-added copy. |

## Declined, on the record

These are decisions, not unfinished work. Each reason is written into the tree
so it is not re-filed as drift.

| Topic | Why |
|---|---|
| Migrate Ubisoft onto `shared/wrapper_session_hooks` | `SPECS` has only a `battlenet` row, so the mixin is **inert** for Ubisoft. Migrating swaps working behaviour for a no-op. |
| Migrate Ubisoft onto `shared/auth_shortcut` | ~580 lines into `shared/` for one consumer, on the store with the longest sign-in incident history. **Re-affirmed 2026-08-26.** Revisit when a third wrapper store arrives — that is the trigger. |
| `ubisoft_recovery.clone_template_into` is not the shared clone | Runs under system Python, synchronous, needs a `cp -a` fallback, and restores the target's own Proton marker across the copy. |
| `ARTWORK_REQUEST` kept with no emitter | Deliberate entry point for a force-refetch trigger. Marked `# unwired:`. |
| `PLAYTIME_SYNC_COMPLETE` / `_FAILED` kept unwired | Surfacing "your playtime reached GOG" is wanted; it has no UI yet. |
| `cloud.failure_behavior` RPCs deleted rather than wired | **Reasoning invalidated 2026-08-26** — it rested on `cloud_failure.py` being live, and it is not (**37**). Re-open as a product question, not a code one. |

## New items found 2026-08-26

Found by re-deriving against the tree rather than reading the register.

| ID | Title | State |
|---|---|---|
| 34 | Shadow-package trap: `launcher/fixes/` + `launcher/language_setup/` | **CLOSED** — 13 stub files deleted 2026-08-26; gates green, real `launcher/proton/*` intact. |
| 35 | `inject_game_to_appinfo` wastes an RPC per overview read | VALIDATING — deleted 2026-08-26: the stub, its `rpcRoutes` row, and both round-trips. Kept the local `forceInjectMetadataForShortcut`, which does the real work, renamed the wrapper `reinjectMetadataWhenLoaded`, and gated the hot `GetAppOverviewByAppID` path on `patchedOverviews` so it no longer re-spoofs on every read. Persistence was redundant, not missing: `applyAppStorePatch` re-spoofs from the backend cache on every plugin load. **Validate:** DV-S. |
| 36 | A `%command%`-leading `LaunchOptions` never launches, and sync preserves it | VALIDATING — fixed 2026-08-26 in `rewrite_for_sync`, the one path that can heal an already-broken shortcut. Dropping a `%command%` that nothing precedes is lossless: it is only meaningful as a separator, and `mangohud %command% gog:123` is untouched. 6 tests covering both the repaired and the must-not-touch forms. **Validate:** DV-T. |
| 37 | Cloud-sync failures were silent | VALIDATING — **decided 2026-08-26: upload failures surface.** The module was fully written and unimported; the missing call was at `services/launcher/helpers.py:372`, a bare `logger.warning(... ignoring ...)`. A failed upload was **completely silent**: the user quit believing progress had reached the cloud. Two traps found before shipping it, both of which would have gone out with the wiring: the strings interpolate `{{error}}` while the payload sends `error_code` + `error_i18n_key`, so the message would have read *"…failed for gog ()."* — now resolved by a shared `buildToastParams`, which also de-duplicated the two renderers' param logic. `# unimported:` marker removed. **Validate:** DV-O. |
| 38 | `set_bus_collaborators` has zero callers (check 4 misses sync methods) | CLOSED — deleted 2026-08-26; `pipeline_factory` already assigned those attributes directly. **Check 4 was NOT widened**: its docstring is right that a sync method is not RPC surface, so reporting one as a dead *RPC* would be a false claim. The blind spot is real but belongs to check 12 / vulture, not to the RPC arm. |
| 39 | `ROW_CONFIG` waits for status strings no backend emits | VALIDATING — deleted 2026-08-26, and it was worse than recorded. It could **never** fire: `StoreStatus` is a closed union of `connected \| disconnected \| expired \| error`, so `status === "legendary_not_installed"` compared against a value the type cannot hold. It also covered 2 of the 3 CLI stores — GOG gained a `CLITool` in the §3.2 pass and never got a row. Removed with its two locale keys; the capability is refiled as **50**. **Validate:** DV-U1. |
| **50** | **No store tells the user which bundled CLI is missing** | OPEN | Opened by 39. A lost exec bit is a real failure mode (`scripts/ensure_executable_bits.py` exists for it), and since §3.5 a missing `gogdl` makes GOG unavailable — which the stale-sweep fix now makes safe but still silent. Needs a real reason on the `check_store_status` payload for **all three** CLI stores, not a frontend-only map. |
| 40 | `assert_all_keys_resolve` has zero callers; CI comments describe it as the boot check | VALIDATING — fixed 2026-08-26. Deleted the strict variant and `KeyPresenceError` (44 lines, zero callers, docstring claiming it ran at boot and was fatal). `collect_missing_keys` **is** the boot check and deliberately warns into degraded mode rather than aborting. Corrected **five** false statements: its own docstring, `collect_missing_keys`' docstring, three in `check_config_keys.py`, one in `quality.yml`. **Validate:** none needed. |
| 41 | Two dead defensive fallbacks on `POST_SYNC_PHASE_CHANGED` | CLOSED — deleted 2026-08-26. All four emitters send `sync_kwargs` or omit it; no emitter has ever sent a flat `games`/`is_force`. `TOLERATED_SUBSCRIBER_READS` is back to empty, which is the goal — an exemption is a place for a defect to hide. |
| 46 | Circuit breaker never resets on success | VALIDATING — see the P0 table above; needs DV-L1 **Validate:** DV-L. |
| 47 | `SHARED_HELPERS` is name-exact, so a renamed copy escapes check 11 | VALIDATING | Closed 2026-08-26 with **check 13**, matching on body *shape* — identifiers and literals erased, structure and attribute names kept — so a rename cannot hide a copy. Verified against a planted `_appid_key_candidates`, the exact historical escape. **A name-variant matcher was tried first and rejected on measurement**: it fired on seven unrelated `_write_marker*` functions with different signatures, the `fix_pfx_symlink` trap of §3.3, and a gate that reds untouched code gets switched off. Body-shape found **16 real groups** over 2357 functions, grandfathered shrink-only in `duplicate_bodies_baseline.json` — including the `epic/sessions.py` ↔ `epic/achievements.py` mirroring the convergence map flagged, and a genuine twin pair (`winetricks._write_marker` ↔ `epic_prerequisites._write_marker_sync`). 7 tests. **Validate:** DV-V. |

## Convergence backlog (check 13)

Machine-tracked in `scripts/duplicate_bodies_baseline.json`, **shrink-only**.
Found by body shape, so a renamed copy cannot hide. 16 groups at baseline,
**1 remaining**, and that one is a deliberate keep — so the backlog is
effectively drained.

Four of the closures turned out to be **defects, not just duplication**: the
cloud-save title matcher (an arbitrary folder returned as a game's save dir),
Epic's token refresh (`UnboundLocalError` masking a missing legendary binary,
plus an uncleaned environment), the achievements error tuple (a third store's
failure would escape unhandled), and the GOG timestamp parser (correct only
by accident of the interpreter). None was visible from the duplicate report
alone; each surfaced from reading the copies side by side, which is the whole
argument for §3.4's method of diffing before merging.

Two rows below reverse a verdict recorded here earlier. Both reversals are
marked ⚠ and carry their reasoning, because a register that quietly rewrites
its own decisions is worth less than one that shows them changing.

| Group | Verdict |
|---|---|
| ~~`normalize_prefix_root` ×6~~ | **DONE 2026-08-26.** Three renamed `_prefix_root(plan)` in `proton/compat/` and three under the canonical name in `proton/fixes/` — all six sitting *beside* `infrastructure/prefix_layout.py`, which already owned the helper. Now pinned in `SHARED_HELPERS`. |
| ~~`resolve_drive_c` ×2~~ | **DONE.** Same story; the better docstring (the umu `pfx -> .` self-symlink that made Ubisoft recovery miss a real `upc.exe`) was promoted onto the canonical version rather than deleted with the copy. Four consumers repointed. |
| ~~`_client_dir` / `client_dir` ×2~~ | **DONE 2026-08-26.** |
| `find_client_exe` / `find_launcher_exe` ×2 of 6 | **DONE.** The launcher's `battlenet_client.py` reimplemented the store's path logic — three lookups **and** its own `CLIENT_DIR`/`CLIENT_EXE`/`LAUNCHER_EXE` string literals, which check 13 cannot see because constants are not function bodies. All now delegate to `stores/battlenet/paths.py`. The import **must** stay function-local: the store package reaches back into the launcher via `prefix_layout`, so a module-level import closes a cycle — which is exactly what my first attempt did, breaking `test_battlenet_agent_queue` collection. `battlenet_bootstrap` reaches into the store the same way for the same reason. |
| ~~`find_upc_in` + 3 store-side lookups ×4~~ | **DONE 2026-08-28 — group dissolved.** The three store-side lookups (`client_dir`, `client_exe`, `launcher_exe`, plus `client_config` which check 13 had *not* flagged) fold onto a module-private `_existing(base, name, predicate)` in `stores/battlenet/paths.py`. `find_upc_in` is deliberately **not** folded in: it lives in the launcher layer, and sharing would not prevent the bug its docstring records. That incident was *choosing the wrong base* — combining `prefix_root / "drive_c"` directly instead of going through `resolve_drive_c` — and the base stays the caller's choice either way. §3.3 claim 3 (sharing is an improvement) fails; deduplicating four lines across a layer boundary would have bought nothing and implied a safety it does not provide. |
| ~~`_kill_wineserver` ×2~~ | **DONE 2026-08-26.** Promoted to `wineserver_reap.py` as `kill_wineserver(..., context=)` — the log label was the only difference. Kept distinct from `reap_prefix_wineserver`, which is the forceful by-PID path for a server that has already detached; this one has a wine binary and asks nicely. |
| ~~epic `_parse_ts` / `_resolve_auth` / `_is_expired` ×2 (3 groups)~~ | **DONE 2026-08-28 — and the measured diff found a live bug.** `sessions.py`'s header declared the duplication deliberate: *"Kept self-contained rather than shared with achievements to avoid disturbing that working path; the resolver is small and stable."* Diffing the six functions is what overturned it. Three pairs were byte-identical apart from docstrings and line wrapping — nothing to disturb. But `_refresh_token`, which check 13 did **not** group, had drifted, and **each copy was missing a fix the other had**: <br>· achievements passed no `env`, so `legendary status` inherited the plugin environment `clean_cli_env` exists to strip (the `LD_LIBRARY_PATH` → `rc=127` class); <br>· achievements referenced `proc` in its `except` without binding it first, so a `FileNotFoundError` from `create_subprocess_exec` — a stale `cli_path`, which the `if not self._cli_path` guard does not catch, since it only checks the string is non-empty — raised **`UnboundLocalError` out of the handler** and masked the real error. Verified by running the old body: it does raise; <br>· sessions passed no `stdin`, leaving the child attached to the parent's, where legendary's ungated `input()` prompts (UD-026) wait instead of failing; <br>· sessions logged no return code, so a refresh that ran and failed looked exactly like one that worked. <br>New `stores/epic/launcher_auth.LegendaryLauncherAuth` mixin takes all four, so it is **neither former copy** — §3.2 again, and this time neither side was the direction. `sessions.py` 181 → 118 LOC. 15 tests. |
| ~~`_parse_ts` ×3 (epic ×2, gog)~~ | **DONE 2026-08-28.** Now `stores/shared/timestamps.parse_timestamp`. GOG's copy escaped check 13 by one call — it omitted `.replace("Z", "+00:00")`. Harmless on the interpreter it runs under (the backend is Python 3.11, where `fromisoformat` accepts `Z` natively) but harmless *by accident*, and the launcher's Python can be 3.10. The shared version keeps the normalisation so the answer no longer depends on which interpreter is running. |
| ~~`EpicAchievementsError.__init__` / `GOGAchievementsError.__init__`~~ | **DONE 2026-08-28.** Identical constructors on two `Exception` subclasses that `rpc/mixins/achievements.py` already caught **as a tuple** — the tell that two names describe one concept. New `stores/shared/achievements_error.StoreAchievementsError` base; both keep their names, since a bare base would lose which backend failed. The RPC now catches the **base**, which is the actual win: a third store's achievements failure is handled the day it is written, where under the tuple it would have escaped unhandled for that store alone. |
| ~~`_list` ×2 (`gog`, `microsoft`)~~ | **DONE 2026-08-26**, and it was **nine** closures, not two. Both stores defined `_s`/`_i`/`_list` inside `from_config_manager`, byte-identical apart from the captured prefix. Check 13 saw only `_list`: `_s` and `_i` sit under its body-size floor — the floor's honest cost, now recorded. New `stores/shared/config_reader.StoreConfigReader(config, prefix)`, 18 tests. The wrapper stores' `_FIELD_SPECS` tables are deliberately left alone: declaring *which* keys exist is a different job from coercing one. |
| ~~`_read_real_steam_id` ×2~~ | **DONE 2026-08-28.** New `core/steam_appid_map.read_positive_steam_appid`, 14 tests. There are **five** readers of the `steam_real_appid` namespace, and only these two shared a contract, so only these two folded — the direction came from reading all five, not from the pair. The other three preserve the `-1` "no Steam counterpart" sentinel that the sync partition uses to skip a game; this one collapses it to `0`, because its callers only ask "is there an AppID to look up". Folding further would have broken that signal. The two copies also tried only the **signed** AppID form — correct for their callers, who pass `Game.app_id`, so a robustness gap rather than a live defect; the shared version tries both via `appid_candidates`. |
| ~~`_match_child_by_title` / `_match_title_dir` ×2~~ | **DONE 2026-08-28 — and it was a live defect.** The two guarded the empty title differently: `status.py` checked the **sanitised** value, `gog_strategy` the **raw** one. So a GOG title with no ASCII alphanumerics (a non-Latin release, or one that is all punctuation) sanitised to `""`, passed the guard, and matched on `"" in child_name` — returning **the first directory under `Saved Games`** as the game's save dir, which cloud sync then uploads and a restore writes back over. The mirror case (a punctuation-only *child* dir matching every title) was present in both. Now `path_resolver.find_save_dir_by_title`, which also owns the four candidate roots both callers duplicated and sorts the scan so a heuristic that picks "the first match" picks the same one twice. 15 tests. Textbook §3.2: a drift finding names a difference, not a direction — here the direction was only visible with both side by side. |
| ~~`_load_mapping` ×2~~ | ⚠ **REVERSED — folded 2026-08-28.** This row previously read *"Do not fold: both live in services that item **4i** may delete entirely; folding them first would be work thrown away."* That reasoning assumed the helper would land somewhere probe-specific. It went into `utils/config_helpers.py` instead — the module that exists *because* this exact class of forgiving config read was copy-pasted 13 times — so it stands on its own and is useful to any future caller. If 4i does delete both services, the cost is one unused function, which check 12 and vulture will surface; the benefit meanwhile is that the gate now blocks a third copy. Also worth recording as the tree's clearest statement of the problem: one copy **cited the other in a comment** (*"same structural fix as `ProbeReactionService._load_mapping`"*) instead of sharing it. Knowing about a duplicate is not the same as being stopped from keeping it. 18 tests. |
| ~~`emit_circuit_open_toast` / `emit_launcher_error_toast`~~ | ⚠ **REVERSED — folded 2026-08-28.** This row previously read *"Keep. Same shape, different message and different failure. Parallel by design, not a copy."* True of the *messages*, wrong about what was duplicated: both docstrings independently re-explained, at length, why the toast must ride `LAUNCHER_STAGE` — that the launcher subprocess's bus dies with it and `frontend_bridge` forwards that channel and nothing else. That constraint is what a third copy would most likely get wrong, and getting it wrong is **silent**: both functions previously emitted `TOAST_NOTIFICATION`, which had no forwarder and no subscriber, so a circuit-breaker refusal and a terminal launch failure produced no message anywhere. Duplicated *knowledge* with a silent failure mode is worth folding even when the payloads differ. Now `error_toasts.emit_launch_error_toast(i18n_key=, extra_params=, tag=)`, with the reasoning stated once. `helpers._emit_cloud_unavailable_toast` is deliberately left out: low-priority *warning* about the host Python, different titling, not a failure of this launch. |
| `_require_*` ×4 (RPC service accessors) | **Keep.** Four one-line accessors that must each name their own service; a shared one would need the name passed in and read worse. |

## Gate blind spots to close

Each closes a defect class the audit hit repeatedly. These are the durable half.

| ID | Gate | First-run expectation |
|---|---|---|
| G-C1 | ✅ **DONE** — check 3 added, locale→code | 271 grandfathered in `scripts/i18n_unused_baseline.json` (**shrink-only**); a new dead key fails immediately. 8 tests. Verified: planting one key in all 16 locales exits 1; 85 backend-named keys correctly excluded. |
| G-C2 | ✅ **DONE** — subscribe-side arm added | Found exactly the predicted 3 over 57 handlers: the P0 (**46**) and two dead fallbacks (**41**, in `TOLERATED_SUBSCRIBER_READS`). 6 tests. Verified: re-planting `rc` fails the gate. |
| G-C3 | ✅ **DONE** — check 12 added | 9 orphans found, 8 deleted, 1 marked `# unimported:` (item **37**). 10 tests. Verified: a planted orphan fails check 12 while vulture at 80 stays silent. |
| G-C4 | ❌ **NOT DONE — and should not be** | Check 4's docstring is correct that a sync method is not RPC surface, so reporting one as a dead *RPC* would be a false claim. The one instance (**38**) was a redundant setter and is deleted. The real blind spot (a public method nothing calls) belongs to vulture/check 12. |
| G-C5 | Check 11 is name-exact | items **20**, **47** |
| G-C6 | Capability parity between the `get_store_infos` payload and `src/` | items **26**, **31** |

Every gate is tuned against **planted violations**, not just a clean tree. That
is the house rule and the reason the existing eleven checks survived.

---

## How to work this file

1. Pick a row. Re-derive it against the tree before doing anything — the
   2026-08-24 review found that of eleven lines in one section, one was outright
   wrong, one stale, one understated by an order of magnitude, and the two
   costliest defects were absent. **Do not work the list; work the tree.**
2. When the fix lands and gates pass, set `VALIDATING` and add its steps to
   `device-validation.md`.
3. When the steps pass on a Deck, set `CLOSED`.
4. If you decline it, move it to the declined table **and write the reason into
   the code**, not just here.
