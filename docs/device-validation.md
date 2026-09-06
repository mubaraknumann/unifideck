# Device-validation ledger

> Every `VALIDATING` row in `docs/audit-register.md` points here. A fix is not
> closed until its steps pass **on a Steam Deck**, per
> `validation-means-user-installs-and-tests`.
>
> Consolidated 2026-08-26 from 13 separate session plans in `~/.claude/plans/`,
> each of which restarted its own `V`/`TV`/`D` numbering — `TV-1` meant five
> different tests. **IDs here are globally unique and permanent.**

## How to run

```bash
pnpm run build && ./build-plugin.sh dev quick-install
sudo systemctl restart plugin_loader
# logs: ~/homebrew/logs/Unifideck/   ·  bundles: QAM Settings → Capture Logs
```

Record what you actually observed in the Evidence column, not "ok". A step with
no evidence is not a passed step.

**⚠ Before running anything marked `DESTRUCTIVE`, ask.** Those can delete a real
install.

## Where to start

There are 164 steps here. Do **not** work top to bottom — that order is by
register item, not by value, and most of it is regression cover for changes
that are very unlikely to have broken anything.

**A first session is 22 steps and proves most of the programme:**

1. `pnpm run build && ./build-plugin.sh dev quick-install`, then
   `sudo systemctl restart plugin_loader`.
2. **SW1–SW5** — the standing sweep. If any of these fail, stop and report;
   nothing below is meaningful on a build that cannot sync or launch.
3. The **17 decisive steps** listed at the bottom of this file. Each one is a
   step whose failure would mean a change was wrong, rather than merely
   unconfirmed.

That is the honest minimum before anything moves to `CLOSED`. Everything else
can be worked in group order afterwards, or skipped for groups covering
changes you are confident in — as long as the register row stays `VALIDATING`
and does not get ticked.

**Three of the recipes below leave state behind if you stop half-way** — 1 (a
renamed game directory), 2 (a failure threshold of 1) and 6 (an older save
restored over the live one). Each says how to undo it. Undo before you stop,
not at the end of the session.

---

## Inducing the conditions

Many steps below say "trip the breaker" or "force a token refresh" without
saying **how**. These are the recipes, derived from the code rather than
guessed. Every one is reversible and the undo is given with it.

Do these on a game you do not mind disturbing. Nothing here deletes anything,
but recipe 6 touches real save files.

### 1. Make a launch fail on demand
*Needed by DV-B1, DV-B2, DV-L1, DV-M1, DV-M3, DV-M5, DV-V7, DV-V8.*

Move the game's executable aside — the launcher then fails at exec. Renaming
the **directory** rather than the file is what makes it deterministic: a stale
`games.map` row alone is not enough, because the launcher re-resolves the exe
from the install directory when the row looks wrong.

```bash
grep '^gog:' ~/.local/share/unifideck/games.map        # pick a game, note its dir
mv "/path/to/Game Dir" "/path/to/Game Dir.OFF"          # undo: mv it back
```
Press Play. Undo by renaming back — do this **before** you finish testing, or
the game reads as uninstalled on the next sync.

### 2. Trip the circuit breaker in one launch instead of three
*Needed by DV-B1, DV-L1, DV-M1, DV-M3, DV-M5, DV-V7.*

The threshold and window are config-driven, so you do not have to fail three
launches inside ten minutes. Edit `~/.config/unifideck/config.json` (it exists
and currently holds only `download` / `steam` / `ui` — add this block), then
`sudo systemctl restart plugin_loader`:

```json
"circuit_breaker": { "failures_threshold": 1, "window_seconds": 600 }
```

One failed launch (recipe 1) now opens the breaker. **Remove the block and
restart when finished** — leaving it at 1 makes any single crash refuse the
next launch. Defaults are `3` / `600` / `fast_boot_seconds: 10`.

### 3. Force an Epic token refresh
*Needed by DV-V5.*

Do not wait for the token to age out. Set its expiry into the past:

```bash
cp ~/.config/legendary/user.json ~/.config/legendary/user.json.bak
python3 -c "import json,pathlib;p=pathlib.Path.home()/'.config/legendary/user.json';\
d=json.loads(p.read_text());d['expires_at']='2020-01-01T00:00:00.000Z';p.write_text(json.dumps(d))"
```
Then open the Epic achievements panel. `legendary status` rewrites the file, so
it repairs itself; the `.bak` is there if the refresh fails outright.

### 4. Take the network down
*Needed by DV-D4, DV-F5, DV-J1, DV-O1, DV-P1.*

```bash
nmcli networking off     # undo: nmcli networking on
```
Timing matters for the mid-operation steps — start the sync or install first,
then drop the network while it is running.

### 5. Make one store fail to answer a sync
*Needed by DV-J1, DV-D4.*

Sign that store out from **QAM → Store Connections**, then Force Sync. This is
the important one for DV-J1: the store must keep its shortcuts, not lose them.
Sign back in afterwards.

### 6. Cloud-save conflicts — the two kinds are induced differently
*Needed by DV-O1, DV-P2, DV-P4, DV-P5.*

**Back up the save directory first** (`cloud_sync_state.json` lists the resolved
path per game). These are the only recipes here that touch real save data.

- **HARD** (plain error toast, no pick): empty the local save directory before
  quitting. The strategy refuses to push nothing over a real cloud copy.
- **SOFT** (the pick modal, DV-P4): the local saves must exist but look
  *regressed* against the cloud. Play so a cloud copy exists, restore an older
  copy from `~/.local/share/unifideck/save_backups/` over the live directory,
  then quit.

DV-P5 is the pair to DV-P4 and needs no setup beyond recipe 4: a network-down
upload failure must produce a **plain toast and no modal**.

### 7. Stall a CLI install
*Needed by DV-F6, DV-F7, DV-F8.*

Already spelled out in those rows: `pkill -STOP -f gogdl` (or `legendary` /
`nile`) part-way through, then `pkill -CONT -f gogdl` to resume. Under 120s
nothing should happen; past 120s it fails with a localized stall message.

### Not yet specified

I could not derive a safe, repeatable recipe for these four. Treat them as
blocked rather than failed if you cannot reach the condition:

| Step | What is missing |
|---|---|
| DV-B3 | How to refuse a `shortcuts.vdf` write without risking Steam's own writes |
| DV-H2 | How to fail a Ubisoft install deterministically — UPC owns the download |
| DV-J6 | How to reach the Microsoft install path, which is guarded twice on the way in |
| DV-P2 | How to fill the save volume safely enough to be worth it |

## Status key

`( )` not run · `(P)` passed · `(F)` failed · `(B)` blocked · `(R)` retired

---

## Standing post-change sweep — SW1…SW5

Run after **any** change in this programme. These replace the near-identical
"nothing else moved" steps that appeared separately in five different plans.

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| SW1 | Full library sync across all seven stores | Reconcile tally line: no unexpected `removed=`; game count unchanged | ( ) | |
| SW2 | Open App Details for one game per store | Panel renders; no missing metadata, size or artwork | ( ) | |
| SW3 | Launch one already-installed game | Launches; correct per-game prefix in `game.log` | ( ) | |
| SW4 | QAM → Store Connections after `systemctl restart plugin_loader` | All six rows, correct connected/disconnected state | ( ) | |
| SW5 | QAM → Capture Logs | Bundle builds clean; no traceback, **and it contains `launches/*.log`** — item 4j deleted `LaunchLogsService.export` on the grounds that the bundle already carries those files, so this assertion is what keeps that true | ( ) | |

---

## DV-A — item 1, `GAME_INSTALLED` retired

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| DV-A1 | Install any game, then check the `[Unifideck] Installed` collection | The game appears **without** a full re-sync — this is the bug the retirement fixed | ( ) | |

## DV-B — item 3, `TOAST_NOTIFICATION` → `LAUNCHER_STAGE`

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| DV-B1 | Trip the circuit breaker (3 failed launches in 10 min), press Play | A toast naming the game, not silence | ( ) | |
| DV-B2 | Force a terminal `LauncherError` | Toast with the error's own key | ( ) | |
| DV-B3 | Force a shortcut-write refusal | Toast, visible for ~12s not 7.5s | ( ) | |
| DV-B4 | Existing launcher toasts | No regression | ( ) | |
| DV-B5 | Repeat DV-B1 in **Gaming Mode** | Toast renders there too | ( ) | |

## DV-C — item 4, `DOWNLOAD_*` single-emitter

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| DV-C1 | Fail an **Epic** install | **One** toast, carrying the game's name (was two, one unnamed) | ( ) | |
| DV-C2 | Fail an **Amazon** install | Same shape | ( ) | |
| DV-C3 | Successful Epic install | `download_completed` increments **once** in the bundle | ( ) | |
| DV-C4 | Successful Amazon install | Same | ( ) | |
| DV-C5 | GOG control | Unchanged — proves nothing moved for the other four stores | ( ) | |
| DV-C6 | Cancel an install | No `_pending_timers` leak; `download_duration_ms` absent, not wrong | ( ) | |
| DV-C7 | Epic **update** path | Same as DV-C1/C3 | ( ) | |
| DV-C8 | Ubisoft + Battle.net install smoke test | Manual phase still indeterminate, no regression | ( ) | |

## DV-D — §1.3, event-bus mismatches

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| **DV-D1** | **Suspend mid-game and wake** — play ~2 min, suspend ~10 min, wake, quit | Session records ~2 min, **not ~12**. The one that must pass. | ( ) | |
| DV-D2 | Play across local midnight | Day attribution unchanged | ( ) | |
| DV-D3 | Ordinary session | Still recorded | ( ) | |
| DV-D4 | Make the Game Pass subscription probe fail (drop network mid-sync) | A toast explains the skip; the xCloud library is not silently dropped | ( ) | |
| DV-D5 | Sync the other five stores | Skip toast does **not** fire for them | ( ) | |
| DV-D6 | Sign in to Battle.net | Its sign-in tile has artwork, not a bare tile | ( ) | |

## DV-E — item 5, `merge_install_status` + browser-auth rebuild

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| DV-E1 | Install an Epic game → Force Sync | Tile stays INSTALLED and launches | ( ) | |
| DV-E2 | `rm -rf` an installed Epic game's directory by hand → Force Sync | Tile flips to **not installed** (the strict-check convergence) | ( ) | |
| **DV-E3** | **GOG installed → Force Sync** | Still INSTALLED, `exe_path` intact — the one thing consolidation could break | ( ) | |
| DV-E4 | Amazon install → Force Sync | INSTALLED and launches | ( ) | |
| DV-E5 | Blank one entry's `path` in `~/.config/nile/installed.json` → sync | That row is **not** marked installed (the deliberate behaviour change) | ( ) | |
| DV-E6 | Sign out and back in via QAM on one browser-auth store | Full CDP flow completes | ( ) | |
| DV-E7 | Ubisoft sign-in from the QAM | Works — proves its own hook was not swept into the shared mixin | ( ) | |
| **DV-E8** | **Start a GOG install after a plugin restart** | Spawns — proves `_after_auth_flow_built` still populates `_gogdl_bin`. Highest-risk row. | ( ) | |
| DV-E9 | Boot log after restart | `[prefix_bridge] reclaimed …` present | ( ) | |
| DV-E10 | QAM compatdata cleanup panel | Every prefix size matches the shared `dir_size_bytes` walk | ( ) | |

## DV-F — item 6, CLI-store drift

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| DV-F1 | Cancel a **GOG** install at ~20% | Row flips to Cancelled **and no `gogdl` process survives** | ( ) | |
| DV-F2 | Cancel an **Amazon** install, stopwatch it | Returns in **under ~2s** (was blocking up to 3600s) and no `nile` survives | ( ) | |
| DV-F3 | Cancel an **Epic** install | Control — unchanged, still clean | ( ) | |
| DV-F4 | Uninstall the DV-F1 game, then `ls` its directory | Gone and stays gone — no orphan rewriting files | ( ) | |
| DV-F5 | Drop Wi-Fi during a GOG install | Error is **localized and specific**, not the bare token `download_failed` | ( ) | |
| DV-F5b | Repeat DV-F5 with a non-English UI language | Still localized | ( ) | |
| DV-F6 | `pkill -STOP -f gogdl` at ~30%, resume at 60s | Nothing happens — a live-but-slow install is never killed | ( ) | |
| DV-F7 | Same, left stopped past 120s | Fails with a stall message at ~120s, and the message is **localized** — item 28 added `errors.download.stalled`; before it, the one failure that reached the user as raw English was `stalled: no output for 120s while downloading` | ( ) | |
| **DV-F8** | **Repeat DV-F7 on Epic, then Amazon** | Each fails at ~120s — **the new behaviour**; these two had no stall detection at all | ( ) | |
| DV-F9 | Large GOG install through extraction | Not killed during the quiet tail (finalize window) | ( ) | |
| DV-F10 | One install on each of GOG/Epic/Amazon, screenshot at ~50% | No negative transfer rate; consistent formatting | ( ) | |
| DV-F11 | `grep -i 'BinaryResolver.*gogdl' ~/homebrew/logs/Unifideck/*.log` | Tier-1 hit — GOG now goes through the resolver, so SHA256 and the exec-bit test run | ( ) | |
| DV-F12 | Sign out and in on Epic and Amazon, then `stat -c '%a %n' ~/.config/{legendary,nile}/user.json` | Both `600` | ( ) | |

## DV-G — item 7, wrapper/CLI tables

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| DV-G1 | QAM → Store Connections | Six rows | ( ) | |
| DV-G2 | `storeInfoStore.getSnapshot()` in the console | `uses_wine` **absent**, `client_runs_in_prefix` **present** | ( ) | |
| DV-G3 | Launch a Ubisoft game | Skips the generic redistributable step (~90s saved) | ( ) | |
| DV-G4 | Launch a GOG game | Still **runs** generic compat — the inverse guard | ( ) | |
| DV-G5 | Wrapper-store install | Progress is indeterminate (manual phase), not a fake % | ( ) | |
| DV-G6 | Cart button on Ubisoft and Battle.net | Opens the client storefront | ( ) | |
| DV-G7 | Cart button on Epic | Opens the Edge storefront | ( ) | |
| DV-G8 | Sign out/in on one wrapper and one CLI store | Both complete | ( ) | |
| DV-G9 | **DESTRUCTIVE** — Proton-family-change regression guard | Prefix reset does **not** delete a Ubisoft install. **Ask before running.** | ( ) | |

## DV-H — item 8, wrapper-store drift (Ubisoft)

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| DV-H1 | Cancel a Ubisoft install mid-download | UPC's own logs **survive** in `launches/*.vendor.txt` | ( ) | |
| DV-H2 | Force a Ubisoft install to fail | Same | ( ) | |
| DV-H3 | Capture Logs after DV-H1 | Bundle contains the `.vendor.txt` | ( ) | |
| DV-H4 | Cancel before the UPC window appears | Nothing to salvage is not an error | ( ) | |
| **DV-H5** | **Identity repair on an installed Ubisoft game** (the `--checksum` proof) | The identity files are actually rewritten, not skipped by the quick check | ( ) | |
| DV-H6 | Fresh Ubisoft install | First clone not slowed (~12s / 1.6 GB band) | ( ) | |
| DV-H7 | Sign out of Ubisoft, sign back in | `deriving template …` — template refresh realigns | ( ) | |
| **DV-H8** | **Play a Ubisoft game ~1 min, quit via Steam** | Capture waits for UPC to exit, then succeeds — no torn vault read | ( ) | |
| DV-H9 | Immediately launch a **different** Ubisoft game | Opens already signed in — the symptom DV-H8 prevents | ( ) | |
| DV-H10 | During DV-H8, watch playtime and library | The bounded wait does not starve other `GAME_STOPPED` work | ( ) | |
| **DV-H11** | **Legacy markers still read as installed** — check a prefix created before this build | Ubisoft games installed on the old plaintext marker are still detected. **The only step that can regress an existing install, and the precondition for item 43.** | ( ) | |
| DV-H12 | Trigger a fresh clone or repair, read the marker | JSON content, **same filename** | ( ) | |
| DV-H13 | Restart `plugin_loader`, let the orphan sweep run | No prefix reported as unowned | ( ) | |
| DV-H14 | After DV-H1…H13, re-count prefixes | No prefix lost | ( ) | |

## DV-I — items 9 and 12, token persistence and the security split

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| DV-I1 | Epic/Amazon credential permissions after sign-in | `600` | ( ) | |
| DV-I2 | Permissions after a token rotation | Still `600` — the CLIs rewrite at 0644 on every refresh | ( ) | |
| DV-I3 | Sign-out / sign-in round trip | Clean | ( ) | |
| **DV-I4** | **GOG token round trip survives the `EncryptedTokenFile` extraction** | Still signed in after a restart. Flagged highest risk of that pass. | ( ) | |
| DV-I5 | Microsoft/xCloud token round trip | Still signed in | ( ) | |
| DV-I6 | Support bundle `security` block | Reports permission checks for **four** stores through one channel | ( ) | |
| DV-I7 | Force Compatibility on a game | Still resolves to the chosen Proton | ( ) | |
| DV-I8 | Wrapper-store prefix bridging | Cloud saves / size / forensics read the real prefix | ( ) | |
| DV-I9 | Auth shortcut cleanup | Temp sign-in tiles removed after sign-in | ( ) | |
| DV-I10 | Orphan sweep | Removes nothing real. **DESTRUCTIVE-adjacent — ask.** | ( ) | |
| DV-I11 | Uninstall a game installed to a **custom path / SD card** | Manifest still drives the sweep; directory actually goes | ( ) | |

## DV-J — items 11 and 29, partial-implementation flags

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| **DV-J1** | **Make one store fail to answer during a sync** (sign out, or block its network) | Its shortcuts **survive**. The most serious defect in Part 3. | ( ) | |
| DV-J2 | Move `bin/gogdl` aside, sync | GOG shortcuts survive — the regression path the §3.2 fix opened | ( ) | |
| DV-J3 | A genuinely **empty** store | Still swept — the phantom-cleanup case that must not be lost | ( ) | |
| **DV-J4** | **Battle.net library baseline** — record the title count and name the missing F2P/subscription titles | This is a **measurement, not a test**, and it is the precondition for item 29 | ( ) | |
| DV-J5 | Open App Details for an xCloud game | No Install button mounts | ( ) | |
| DV-J6 | Force the Microsoft install path | Refuses with a **translated** message, and the queue row reaches "failed" | ( ) | |
| DV-J7 | Install one Ubisoft and one Battle.net game end to end | Works | ( ) | |

## DV-K — item 23, launch-options parser

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| DV-K1 | Plugin imports after the `dispatcher.py` split | No import error | ( ) | |
| DV-K2 | Version reporting | Reads `package.json` | ( ) | |
| DV-K3 | Account-switch path | Modal still appears | ( ) | |
| **DV-K4** | **Launch a game with NO launch options** | Launches exactly as before. **The regression guard — the one that matters.** | ( ) | |
| DV-K5 | `<store>:<id> MY_VAR=hello` | `MY_VAR` present in `/proc/<pid>/environ` of the **game** process | ( ) | |
| DV-K6 | `<store>:<id> WINEDLLOVERRIDES=…` | User's entry first, Proton's appended after | ( ) | |
| DV-K7 | `<store>:<id> LSFG=1` | `ENABLE_LSFG=1` plus the three `~/lsfg` exports on the game process | ( ) | |
| DV-K8 | `<store>:<id> MY_QUOTED="alpha beta"` | Arrives intact, not truncated to `alpha` | ( ) | |
| DV-K9 | Native Linux game path | Unaffected | ( ) | |
| DV-K10 | Force-Compat re-prepare | Still resolves | ( ) | |
| DV-K11 | *(retired)* wrapper words populate `state.wrappers` | **RETIRED** — item 23b established wrappers are unreachable; Steam applies them pre-exec | (R) | Retired 2026-08-26 |
| DV-K12 | *(retired)* game args populate `state.game_args` | **RETIRED** — deferred to item 23a; wiring it today passes `mangohud` to the game | (R) | Retired 2026-08-26 |

## DV-L — item 46, the circuit breaker resets on success (P0)

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| **DV-L1** | Trip the breaker on one game (3 failed launches inside 10 min), then make it launch successfully and quit normally | The next Play is **not** refused. Log shows `Wiped failures after success for <store>:<id>`. Before the fix this line could never appear. | ( ) | |
| DV-L2 | `cat ~/.local/share/unifideck/launch_history.json` after DV-L1 | The game's `failures` array is gone, not merely expired | ( ) | |
| DV-L3 | Launch and quit a game that was never failing | No failures recorded; no spurious entry created | ( ) | |
| DV-L4 | Press Stop mid-game (signal termination) | Not recorded as a launch failure | ( ) | |

## DV-M — item 4a/4c, the circuit-breaker surface (G2)

Run **after DV-L1** — the badge is only trustworthy once the breaker can
clear (item 46).

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| **DV-M1** | Trip the breaker (3 failed launches in 10 min), then open the game's page | A **"3 recent launch failures"** badge appears beside Space Required, with **Force launch** and **Reset failures** buttons. Before this, the page looked completely normal. | ( ) | |
| DV-M2 | Press **Reset failures** | Badge disappears; the next Play is not refused; log shows `Cleared failures for <store>:<id>` | ( ) | |
| DV-M3 | Trip it again, press **Force launch**, then Play | The launch goes through once (one-shot bypass) | ( ) | |
| DV-M4 | Open a game that has **never** failed | **No badge.** A permanent counter on a healthy game would be noise and would make the badge easy to ignore on an unhealthy one | ( ) | |
| DV-M5 | Trip the breaker, then navigate away and back | Badge still shown — proves the mount-time `get_launch_failures` fallback works, since the event only fires on a *change* | ( ) | |
| DV-M6 | Check the badge text in a non-English UI language | Localized — every string already existed in 16 locales | ( ) | |
| DV-M7 | In Gaming Mode, repeat DV-M1 | Badge and both buttons render and are focusable with the controller | ( ) | |

## DV-N — item 4g, bus supervision is actually wired (G1)

The acceptance signal is a **Capture Logs bundle**, not the code: the
`watchdog` and `dispatcher` blocks read as healthy when they are empty, which
is exactly how this stayed hidden for the life of the project.

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| **DV-N1** | Capture Logs, read `frontend.bus_health.watchdog` | **Non-empty** — one entry per registered handler with an invocation count. It reported `{}` against 42 registered events on 2026-08-25. | ( ) | |
| DV-N2 | Compare against a bundle taken before this change | The `watchdog` block goes from `{}` to populated; nothing else in the bundle changes shape | ( ) | |
| DV-N3 | Ordinary session: boot, sync, open App Details, launch a game | No behaviour change. Supervision is observability; if it alters anything user-visible, that is a regression | ( ) | |
| DV-N4 | `grep -i quarantin ~/homebrew/logs/Unifideck/*.log` | **Nothing.** A quarantine in normal use would mean a handler is timing out repeatedly — a real finding, not a pass | ( ) | |
| DV-N5 | Sync a full library (the heaviest fan-out) | No slowdown; `[DIAG] event=… total=…ms` timings comparable to before | ( ) | |

## DV-O — item 37, cloud-sync upload failures surface

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| **DV-O1** | Make an upload fail: launch a GOG or Epic game with cloud saves, quit, and drop the network (or fill the disk) so `sync_up` raises | A **warning toast**: "Cloud save upload failed for gog (Network unreachable). Your progress is stored locally only." Before this it was silent — log only. | ( ) | |
| **DV-O2** | Read the `{{error}}` half of that toast carefully | A **translated reason** ("Network unreachable"), not an empty pair of brackets and not a raw code. This is the trap the wiring would have shipped. | ( ) | |
| DV-O3 | Repeat DV-O1 with the UI in a non-English language | Both the message and the reason localized (`cloudSync.error.*` covers all 11 codes) | ( ) | |
| DV-O4 | Fill the disk instead of dropping the network | Reason reads "Not enough disk space", i.e. the classifier picks the right code | ( ) | |
| DV-O5 | Set `cloud.failure_behavior.gog = "silent"` in `~/.config/unifideck/config.json`, repeat DV-O1 | **No toast**, log only. The config key survives even though §1.2 deleted its RPCs. | ( ) | |
| DV-O6 | A **successful** launch + quit with cloud saves working | No toast. A false positive here would be worse than the silence it replaces. | ( ) | |
| DV-O7 | Launch a game on a store with no cloud-save support | No toast, no error | ( ) | |

## DV-P — item 4b, toast actions render

Run with **DV-O** (both come from the cloud-failure path).

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| **DV-P1** | Cause an upload failure with the **network down** (DV-O1) | Toast is **clickable**, subtext reads "Retry". Tapping it retries the sync. | ( ) | |
| **DV-P2** | Cause an upload failure with the **disk full** | Subtext "Free up space"; tapping opens Steam's storage settings | ( ) | |
| DV-P3 | Cause an **auth-expired** failure (sign out mid-session) | Subtext "Sign in"; tapping starts that store's sign-in | ( ) | |
| **DV-P4** | Force a genuine cloud-save **conflict** (diverge local and cloud, then quit) | The **pick modal** opens with real numbers on both sides — *not* a plain toast | ( ) | |
| **DV-P5** | Repeat DV-P1 and confirm the modal does **not** open | The trap this guards: `retry-sync` has two producers, and branching on the verb alone would open a pick modal with two empty sides on every dropped Wi-Fi | ( ) | |
| DV-P6 | An ordinary launcher toast with no action | No subtext, not clickable, unchanged | ( ) | |
| DV-P7 | Any toast in a non-English UI | Action label localized | ( ) | |

## DV-Q — items 23a/23b, launch-option arguments

Touches the launch hot path. **DV-Q1 is the one that matters** — everything
else is a feature, this is the regression guard.

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| **DV-Q1** | Launch a game with **no** launch options | Launches exactly as before. Nothing extra on the game's argv. | ( ) | |
| DV-Q2 | `<store>:<id> -windowed` on a game that accepts it | The game receives `-windowed` — read `/proc/<pid>/cmdline` of the game process, not a log line | ( ) | |
| DV-Q3 | `<store>:<id> MY_VAR=1` | `MY_VAR` in the game's environment, and **nothing** added to its argv | ( ) | |
| DV-Q4 | Ubisoft or Battle.net sign-in from the QAM, with `mangohud` in that shortcut's options | Sign-in works; `mangohud` is **not** passed to the client. It used to be preserved into the temp options where it did nothing. | ( ) | |
| DV-Q5 | `mangohud %command% <store>:<id>` | Still works — Steam applies the wrapper pre-exec, unchanged by this | ( ) | |
| DV-Q6 | Launch an Epic and a GOG game (their argv builders both changed) | Both launch; correct per-game prefix | ( ) | |

## DV-R — items 26 and 31, per-store capability flags

The frontend now reads four capability booleans off the `get_store_infos`
payload instead of its own hardcoded store lists. A wrong flag does not throw
— it **hides a feature on a store that has it**, which is exactly how the
deleted `supports_cloud_saves` field behaved for a year.

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| **DV-R1** | Open App Details for a **GOG** and an **Epic** game | Cloud-save UI present on both. This is the regression the old field caused: only Battle.net ever declared it, as `False`, so the two stores that *have* cloud saves both advertised none | ( ) | |
| DV-R2 | Same for Amazon, Ubisoft, Battle.net, Microsoft | No cloud-save UI | ( ) | |
| DV-R3 | Achievements row on a GOG and an Epic game | Present; absent on the other four | ( ) | |
| DV-R4 | Start a **GOG** install, then an **Epic** one | GOG shows the language picker, Epic does not | ( ) | |
| DV-R5 | Store Connections → storefront button per row | Appears only on the browser-storefront stores | ( ) | |
| DV-R6 | `storeInfoStore.getSnapshot()` in the console | Four capability keys present; `icon` and `auth_status` **absent** — both were declared in `api.ts` and never sent | ( ) | |

## DV-S — item 35, the per-overview RPC round-trip removed

`inject_game_to_appinfo` fired from inside the patched `GetAppOverviewByAppID`
getter, so it ran on **every overview read, library-wide**. Deleting it is only
safe because `applyAppStorePatch` re-spoofs on every plugin load.

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| **DV-S1** | `systemctl restart plugin_loader`, then open the library | Non-Steam tiles still carry store artwork and metadata — the re-spoof on load is what replaces the deleted persistence | ( ) | |
| DV-S2 | Open App Details for one game per store | Metadata present, no blank fields | ( ) | |
| DV-S3 | Scroll the library while tailing the log | **No** `inject_game_to_appinfo` traffic. One call per overview read is what this removed | ( ) | |

## DV-T — item 36, a `%command%`-leading shortcut heals

§2.9 measured this launching 0 of 2 attempts, and item 24a's preservation fix
had removed the only thing that used to clean it up.

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| **DV-T1** | Set a shortcut's launch options to `%command% <store>:<id>`, Force Sync, then press Play | The leading `%command%` is gone from the options and the game **launches**. Before this it silently did nothing | ( ) | |
| DV-T2 | A normal `<store>:<id>` shortcut through the same sync | Options untouched | ( ) | |
| DV-T3 | `mangohud %command% <store>:<id>` through the same sync | **Preserved** — this is the legitimate form and must survive. Overlaps DV-Q5 deliberately | ( ) | |

## DV-U — items 39 and 45, store rows and progress labels

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| DV-U1 | QAM → Store Connections, all six rows | Every row renders a status; none blank. `ROW_CONFIG` waited on status strings no backend emits, so its guidance never appeared — confirm nothing regressed by removing it | ( ) | |
| DV-U2 | GOG install through extraction and verification | Progress labels localized; **no** hardcoded English "Extracting…" / "Verifying… 12.3%" | ( ) | |
| DV-U3 | Repeat DV-U2 with a non-English UI language | Still localized — the six deleted producers were English-only restatements of the localized label | ( ) | |

## DV-V — item 47, the convergence closures

Eight duplicate groups were folded, and **four of them turned out to be
defects**. These steps target the four, not the refactor: a pure
consolidation is covered by the standing sweep.

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| **DV-V1** | Play a **GOG** game with cloud saves, then check the resolved save path in the log | It is that game's **own** folder. The GOG title matcher guarded the raw title, so a title with no ASCII alphanumerics matched `"" in child_name` and returned the first directory under `Saved Games` — which sync then uploads and a restore writes over | ( ) | |
| DV-V2 | Same for an **Epic** game with cloud saves | Correct folder; the two matchers are now one | ( ) | |
| **DV-V3** | Open the achievements panel for an **Epic** game | Loads. Both Epic auth copies were merged into `LegendaryLauncherAuth`, and this is the consumer whose copy had the broken error path | ( ) | |
| DV-V4 | Epic playtime sync after a session | Reports. Same mixin, the other consumer | ( ) | |
| DV-V5 | Force an Epic token refresh (play after leaving it idle past expiry) and grep the log | `token refresh done (rc=…)` appears. The sessions copy logged no rc at all, so a refresh that ran and failed looked identical to one that worked | ( ) | |
| DV-V6 | GOG achievements panel | Unlock **timestamps** correct — GOG's timestamp parser was replaced with the one that normalises a trailing `Z` | ( ) | |
| DV-V7 | Trip the circuit breaker, press Play | Toast still appears. Its emitter and the launcher-error emitter now share one delivery function | ( ) | |
| DV-V8 | Force a terminal launcher error | Toast still appears. Overlaps DV-B1/B2 deliberately — both toasts moved in the same commit | ( ) | |
| DV-V9 | Launch a **Battle.net** game | Client resolves and starts; the four `Battle.net.exe` / launcher / config lookups were folded onto one helper | ( ) | |
| DV-V10 | Open App Details for a game with a ProtonDB or Deck-Verified badge | Badge present. The two metadata backfills now try **both** AppID forms where they previously tried only the signed one | ( ) | |

## DV-W — items 20 and 44, lookups and a deleted config key

| ID | Step | Expected | Status | Evidence |
|---|---|---|---|---|
| **DV-W1** | `systemctl restart plugin_loader` and read the boot log | Clean boot, **no missing-config-key error**. Item 44 removed `binary_resolver.version_check_timeout_seconds` from defaults, schema *and* `RUNTIME_REQUIRED_KEYS`; missing one of the three fails boot | ( ) | |
| DV-W2 | Library facets → filter by compatibility rating | Filters correctly. `appid_candidates` was consolidated across four call sites, and a signed/unsigned mismatch here shows as an empty facet, not an error | ( ) | |

---

## Needs no device validation

Recorded so the gap is not re-audited. Each of these is provably inert at
runtime, and the standing sweep covers the rest.

| Item | Why |
|---|---|
| 21 | `OP-XX` markers — comments and docstring prose only; zero occurrences in executable code |
| 24 | Eight deleted modules, each verified to have **no importer** by check 12. SW1–SW5 covers the residual risk |
| 27 | `vulture_whitelist.py` — a lint input, never imported by the plugin |
| 30 | A `NewType` and a mypy check; no runtime behaviour |
| 40 | Deleted dead code plus two corrected CI comments |
| 46 | Covered by **DV-L**, which was written for it |
| 4a, 4c | Covered by **DV-M** |
| 4b | Covered by **DV-P** |
| 37 | Covered by **DV-O** |
| 23a, 23b | Covered by **DV-Q** |

---

## Lost baselines

`~/.claude/plans/…lively-rain.md` required DV-F1…DV-F4 to be captured **once on
the pre-fix build** as a before/after comparison. The build has moved, so that
baseline no longer exists. Run DV-F1…F4 as absolute assertions ("no process
survives") rather than as a comparison — the assertion is the real requirement
and it stands on its own.

## Decisive steps — the 17 that decide a change

Reproduced in full here so a first session needs nothing else on screen. Each
is a step whose **failure would mean a change was wrong**, not merely
unconfirmed. Record the result in the group table above, not here.

⚠ **DV-H11 is the only one that can regress an existing install** — read it
before running it. **DV-J4 is a measurement, not a test**: it produces the
baseline item 29 needs, so there is no pass/fail, only a recorded number.

| # | Step | What must be true |
|---|---|---|
| 1. **DV-D1** | **Suspend mid-game and wake** — play ~2 min, suspend ~10 min, wake, quit | Session records ~2 min, **not ~12**. The one that must pass. |
| 2. **DV-E3** | **GOG installed → Force Sync** | Still INSTALLED, `exe_path` intact — the one thing consolidation could break |
| 3. **DV-E8** | **Start a GOG install after a plugin restart** | Spawns — proves `_after_auth_flow_built` still populates `_gogdl_bin`. Highest-risk row. |
| 4. **DV-F8** | **Repeat DV-F7 on Epic, then Amazon** | Each fails at ~120s — **the new behaviour**; these two had no stall detection at all |
| 5. **DV-H5** | **Identity repair on an installed Ubisoft game** (the `--checksum` proof) | The identity files are actually rewritten, not skipped by the quick check |
| 6. **DV-H8** | **Play a Ubisoft game ~1 min, quit via Steam** | Capture waits for UPC to exit, then succeeds — no torn vault read |
| 7. **DV-H11** | **Legacy markers still read as installed** — check a prefix created before this build | Ubisoft games installed on the old plaintext marker are still detected. **The only step that can regress an existing install, and the precondition for item 43.** |
| 8. **DV-I4** | **GOG token round trip survives the `EncryptedTokenFile` extraction** | Still signed in after a restart. Flagged highest risk of that pass. |
| 9. **DV-J1** | **Make one store fail to answer during a sync** (sign out, or block its network) | Its shortcuts **survive**. The most serious defect in Part 3. |
| 10. **DV-J4** | **Battle.net library baseline** — record the title count and name the missing F2P/subscription titles | This is a **measurement, not a test**, and it is the precondition for item 29 |
| 11. **DV-K4** | **Launch a game with NO launch options** | Launches exactly as before. **The regression guard — the one that matters.** |
| 12. **DV-R1** | Open App Details for a **GOG** and an **Epic** game | Cloud-save UI present on both. This is the regression the old field caused: only Battle.net ever declared it, as `False`, so the two stores that *have* cloud saves both advertised none |
| 13. **DV-S1** | `systemctl restart plugin_loader`, then open the library | Non-Steam tiles still carry store artwork and metadata — the re-spoof on load is what replaces the deleted persistence |
| 14. **DV-T1** | Set a shortcut's launch options to `%command% <store>:<id>`, Force Sync, then press Play | The leading `%command%` is gone from the options and the game **launches**. Before this it silently did nothing |
| 15. **DV-V1** | Play a **GOG** game with cloud saves, then check the resolved save path in the log | It is that game's **own** folder. The GOG title matcher guarded the raw title, so a title with no ASCII alphanumerics matched `"" in child_name` and returned the first directory under `Saved Games` — which sync then uploads and a restore writes over |
| 16. **DV-V3** | Open the achievements panel for an **Epic** game | Loads. Both Epic auth copies were merged into `LegendaryLauncherAuth`, and this is the consumer whose copy had the broken error path |
| 17. **DV-W1** | `systemctl restart plugin_loader` and read the boot log | Clean boot, **no missing-config-key error**. Item 44 removed `binary_resolver.version_check_timeout_seconds` from defaults, schema *and* `RUNTIME_REQUIRED_KEYS`; missing one of the three fails boot |

Setup for these: DV-F8 needs recipe 7, DV-J1 needs recipe 5, DV-T1 needs a
shortcut you have edited by hand. The rest run as written.

---

## Appendix — Steam Machine (DV-SM)

Every step above is written "on a Steam Deck". These are the additional ones
for the Steam Machine work. No Steam Machine hardware is available, so they run
against the forced-device override; DV-SM7 is the only row that genuinely needs
real Fremont hardware.

```bash
pnpm run build && ./build-plugin.sh dev quick-install
sudo systemctl set-environment UNIFIDECK_DEVICE_TYPE=machine
sudo systemctl restart plugin_loader
# after the run:
sudo systemctl unset-environment UNIFIDECK_DEVICE_TYPE && sudo systemctl restart plugin_loader
```

| # | Step | What must be true |
|---|---|---|
| 1. **DV-SM1** | With the override **unset**, open the library and the info panel for a rated game | Byte-identical to before this change: tab reads "Great on Deck", badge and Details modal unchanged. **The regression guard — the one that matters.** |
| 2. **DV-SM2** | Set `=machine`, restart, open the library | Tab reads "Great on Machine", and **exactly one** compat tab exists — no leftover native "Great on Machine" beside ours |
| 3. **DV-SM3** | Compare the tab's contents against the DV-SM1 run | Membership **differs** for titles Valve rates differently per device. Identical lists mean the track never switched |
| 4. **DV-SM4** | Open Details for a game rated on both devices | Title reads "Steam Machine Compatibility"; the test-result rows are the Machine criteria, in the Steam UI language — not the Deck's panel-legibility / APU-performance ones |
| 5. **DV-SM5** | In the CEF console (`steam-debug`), read a shortcut's overview | `steam_machine_compat_category` is **non-zero**. This is the F2 fix; a zero here means Steam's own filters still cannot see our shortcuts |
| 6. **DV-SM6** | Enable collections, boot `=deck`, let `[Unifideck] Great on Deck` be created, then restart `=machine` | The Deck collection **survives** alongside the new Machine one. Before the fix, each device deleted the other's on every boot |
| 7. **DV-SM7** | **On real Steam Machine hardware:** capture a support bundle | `product_name` is `Fremont` and `device_type` is `machine`, with no `device_type_forced`. Also confirms the SteamOS 3-value enum, which is measured but not yet seen on the device it describes |
| 8. **DV-SM8** | Set `=other`, restart | Tab reads "SteamOS Compatible"; badges use the SteamOS wording (`COMPATIBLE`, not `PLAYABLE`) |
| 9. **DV-SM9** | Sync once on a warm cache and watch the log | The schema-2 self-heal re-fetches each cached title **once**; a second sync re-fetches nothing |
| 10. **DV-SM10** | Launch an xCloud title on an external display | The kiosk fills the display instead of a 1024x720 letterbox at 1.25 scale |
