# Feasibility Study: Battle.net (Blizzard) store

> **Status: implemented.** Battle.net shipped as the sixth store (see `stores/battlenet/` and the architecture skill `stores.md`). This document is retained as the historical feasibility record; its implementation caveats are superseded by the shipped code.

**Verdict: FEASIBLE WITH CAVEATS — launcher-wrapper archetype (Ubisoft template).** All four MVP
steps have a working path, but Battle.net is the finickiest of the three: the client self-updates
aggressively and its login window needs TWO specific Proton env workarounds. On this Steam Deck
(2026-07-03) the installer completed unattended and the client installed, but the login window was
**initially unresponsive** → fixed by `WINE_SIMULATE_WRITECOPY=1` (input-responsive, auth
succeeded), then the client **crashed post-login with a generic "Blizzard Error"** → root-caused to
**Xalia** (Proton-GE's gamepad-UI helper) throwing an unhandled exception, fixed by
`PROTON_DISABLE_XALIA=1` (client stable, no crash, reaches the account/password screen). Auth itself
works (config persisted `SavedAccountNames`/`AutoLogin`/`LastLoginRegion=US`). The critical open
question — can we enumerate OWNED (not just installed) games — is answered YES via an authenticated
account endpoint.

No open ownership CLI exists; the client wrapper is the only viable install/launch path, exactly
like Ubisoft. The user has already ported Lutris' Ubisoft library approach — the same play works
here, and Lutris/Playnite both ship maintained Battle.net services to copy the approach from.

## MVP-bar walkthrough

| Step | Status | Evidence |
|------|--------|----------|
| Login | **Proven end-to-end on-device** | Full login completed on this Deck with `WINE_SIMULATE_WRITECOPY=1` + `PROTON_DISABLE_XALIA=1`: login window responsive, auth succeeded, and the **main library window rendered** (signed in as a real account, Online). Web login also drives the account-page session used for ownership. |
| Enumerate owned games | Proven (live reference) + visible on-device | The logged-in client UI showed the owned library ("My Games", "All Games — 33", named titles) — i.e. ownership enumeration is real. Programmatic path: `GET https://account.battle.net/api/games-and-subs` with the authenticated session cookies → JSON `gameAccounts` = owned titles/subs (how Playnite's `BattleNetAccountClient.GetOwnedGames()` works). A ~42-title hardcoded catalog (Playnite `BattleNetGames.cs`; Lutris `GAME_IDS`, ~34) maps product ids → codes/slugs. NOT the official develop.battle.net API (that only exposes per-game WoW/D3/SC2 profiles, no general ownership). |
| Download/install | Proven (live reference) + client verified on-device | Client drives downloads in-prefix. Trigger options: client arg `--exec="install <CODE>"` (Lutris), `battlenet://` deeplink, or the Agent local HTTP API on `127.0.0.1:1120` (`/install/{product}`). `download_phase="manual"` indeterminate progress (Ubisoft precedent). |
| Launch | Proven (live reference) | `--exec="launch <CODE>"` via the client (Lutris) or `battlenet://<CODE>`; client resident, so a watchdog is required (Ubisoft precedent). |

Stretch: no general cloud-save/achievement API for a third-party client (per-game WoW/D3/SC2
profiles exist via develop.battle.net but aren't a unified achievements source) — post-MVP.

## On-device prototype results (2026-07-03, scratch: `~/feasibility-scratch/bnet/`)

- Official `Battle.net-Setup.exe` (from `downloader.battle.net`, 4.9 MB stub) ran under `umu-run`
  with `WINEPREFIX=<scratch>/bnet/prefix`, `PROTONPATH=GE-Proton11-1`. Installer UI rendered
  ("Battle.net Installation", 440 MB required); clicked Continue → downloaded and installed the
  client + Agent to `Program Files (x86)/Battle.net/` (`Battle.net.exe`, `Battle.net Launcher.exe`)
  and `ProgramData/Battle.net/Agent/` (`Agent.exe` 2.40.3.9700, `product.db`). Exit code 0.
- `product.db` present and parseable (confirmed via strings): contains `agent`/`bna` entries,
  region `US`, install paths — the exact structure `lutris.util.battlenet.product_db.ProductDb` /
  `TinkoLiu/blizzard-product-parser` decode. Installed-game enumeration substrate confirmed.
- **Login window: initially unresponsive** (the known Wine white-screen/hang — user could not log
  in). **Relaunching `Battle.net Launcher.exe` with `WINE_SIMULATE_WRITECOPY=1` produced a
  responsive login form** ("Log in or sign up", email/Continue + Google/Facebook/Apple/Discord/
  Xbox/PSN/Nintendo/Steam OAuth buttons); typed input was accepted and advanced to the password
  screen. The user completed login; auth succeeded (config persisted `SavedAccountNames`,
  `AutoLogin=true`, `LastLoginRegion=US`, `LastLoginAddress=us.actual.battle.net`).
- **Post-login crash → root-caused and fixed on-device.** After login the client threw a generic
  "Blizzard Error" (unexpected error dialog). The launch log showed the real cause:
  `Unhandled exception in Xalia: System.Threading.Tasks.TaskCanceledException` in
  `Xalia.Win32.AccessibleProvider.FetchLocation` / `NonclientScrollProvider` — i.e. Proton-GE's
  bundled **Xalia** gamepad-navigation helper crashing while probing the Battle.net window (NOT a
  Battle.net/auth failure). Relaunching with **`PROTON_DISABLE_XALIA=1` (+ `WINE_SIMULATE_WRITECOPY=1`)**
  eliminated the crash: the client ran stably to the "Welcome back" account screen with no Xalia
  exception in the log.
- **Full end-to-end login + library render (final proof).** The user completed login; the client
  loaded its **main library window** signed-in and Online, showing the owned catalog ("My Games",
  "All Games — 33", named titles e.g. Call of Duty: Modern Warfare, Diablo Immortal, Hearthstone,
  Overwatch, StarCraft Remastered) with "Installed — 0" (matching the empty `product.db`). This
  confirms the client is fully usable on-device — the install/launch surface (`--exec` args) is
  reachable. Screenshots: `bnet/logs/bnet-setup-window.png`, `bnet-login.png`,
  `bnet-login-writecopy.png`, `bnet-login-input-test.png`, `bnet-error.png` (the Xalia crash),
  `bnet-noxalia-state.png` (stable after the fix), `bnet-library.png` (logged-in library).
- Ribbit probe (unauthenticated): `GET https://us.version.battle.net/v2/products/wow/versions` →
  region/build/CDN table (global catalog/version infra, clientless). Useful for version metadata,
  NOT per-user ownership.

## Integration design (wrapper archetype, Ubisoft template)

1. **Ownership + catalog**: bundle a small static catalog (~42 Blizzard titles: product id,
   internal id, ngdp code, name — copy the *shape* from Lutris `GAME_IDS`, re-derive values).
   Owned set = `account.battle.net/api/games-and-subs` via the Edge/CDP session cookies captured
   at login (our existing auth harness navigates authenticated URLs — a natural fit). Intersect
   catalog × owned to build the library. Because the catalog is small and stable, this is far less
   work than Ubisoft's Algolia UUID bridge.
2. **Installed state**: parse `ProgramData/Battle.net/Agent/product.db` (protobuf) for installed
   titles + versions + paths — port `blizzard-product-parser` / Lutris' `ProductDb` (the protobuf
   schema is the maintenance surface). Overlay onto the library (satisfies the install-status
   overlay invariant in `stores.md`).
3. **Install/launch**: per-game (or shared) prefix hosting the Battle.net client. Install via
   `--exec="install <CODE>"`; launch via `--exec="launch <CODE>"`, both with a resident-client
   watchdog (`pgrep`-gated, Ubisoft precedent). New `launcher/proton/handlers/battlenet.py` +
   dispatch branch. The Agent HTTP API (`127.0.0.1:1120`, `agent::HttpJsonRouter`, `/install/{p}`)
   is an alternative to CLI args if finer progress is wanted — R&D, not needed for MVP.
4. **Mandatory prefix env (both proven necessary on-device)**: `WINE_SIMULATE_WRITECOPY=1`
   (responsive login) AND `PROTON_DISABLE_XALIA=1` (prevents the post-login Xalia crash); also
   disable Battle.net browser hardware acceleration in `Battle.net.config`. Bake all three into the
   launcher's Battle.net env, analogous to the Ubisoft-specific compat handling
   (`launcher/proton/compat/`). Without `PROTON_DISABLE_XALIA=1` the client crashes right after a
   successful login, so this is gating, not cosmetic.
5. **unifiDB**: `external_ids["battlenet"]` = ngdp/product code. Catalog is small and Blizzard
   titles are well-covered in IGDB, so matching is easy (opposite of itch.io).

## Effort estimate

**Ubisoft-scale wrapper, but the library layer is cheaper: roughly 20–30 files / 5–8k LOC.**
Cost drivers (same as Ubisoft): prefix lifecycle, auth capture, client-update resilience, install
watchdog, manual-phase UX. Savings vs Ubisoft: small static catalog + a clean owned-games endpoint
(no dead API, no Algolia), a documented protobuf parser to port, and IGDB-friendly matching. Add
the mandatory Wine env workarounds. The client's aggressive self-update (the 14542 saga broke
Lutris/Proton repeatedly) makes ongoing maintenance the top risk.

## Risks

- **Client self-update breakage (top risk)**: Battle.net force-updates its Agent/client on launch;
  new versions have repeatedly broken under Wine/Proton (the 14542 incident). Plan a
  pin/repair/kill-sibling-folder strategy and expect periodic maintenance. Directly analogous to
  the EA App churn risk.
- **Login white-screen/hang + post-login crash**: TWO env vars are gating, both confirmed
  on-device — `WINE_SIMULATE_WRITECOPY=1` (unresponsive login) and `PROTON_DISABLE_XALIA=1` (Xalia
  crash after a successful login) — plus disabling browser HW acceleration. Must ship as part of
  the store's compat setup, not left to the user. Xalia is Proton-version-coupled, so a Proton
  bump could change this behavior — pin/validate the Proton used for the Battle.net prefix.
- **Ownership endpoint is unofficial**: `account.battle.net/api/games-and-subs` is an
  account-page endpoint, not a documented API — Blizzard can change it (Playnite tracks it). The
  official develop.battle.net API does NOT provide general ownership, so there's no sanctioned
  alternative; this is inherent.
- **ToS/ban**: Blizzard publicly stated (2025) no bans for Linux/Wine/SteamOS; Warden anti-cheat
  scans in-process for cheats, not the compat layer. No documented bans for running the launcher
  under Wine or automating installs (distinct from cheat bans). Multiplayer/anti-cheat titles'
  Deck-playability varies per title — out of MVP install/launch scope concern.
- **protobuf schema drift**: `product.db` format can change with Agent versions; the parser is a
  maintenance point (small, but real).

## OSS leverage

| Project | License | Health (2026-07-03) | Reuse |
|---------|---------|---------------------|-------|
| lutris/lutris `services/battlenet.py` (308 L) + `util/battlenet/` | GPL-3.0 | pushed 2026-07-03 | Canonical wrapper: `GAME_IDS` catalog shape, `ProductDb` protobuf parser, `--exec="install/launch <CODE>"` idiom, client-required install script. The user already mirrored Lutris for Ubisoft — same approach. GPL: reuse approach/data-shape, keep provenance clean. |
| JosefNemec/PlayniteExtensions `BattleNetLibrary` | MIT | maintained | **The owned-games mechanism**: `BattleNetAccountClient.GetOwnedGames()` → `account.battle.net/api/games-and-subs` (cookie auth) → `gameAccounts`; ~42-title `BattleNetGames.cs` catalog; product.db fallback. MIT — cleanest reference for ownership. |
| TinkoLiu/blizzard-product-parser (+ bartok765/galaxy_blizzard_plugin) | see repo | maintained | Standalone `product.db` protobuf parser to port for installed detection. |
| wowdev.wiki Agent + Ribbit/TACT | docs | current | Agent HTTP API (`127.0.0.1:1120`, endpoints) and Ribbit/TACT protocol reference (clientless catalog; TACT-direct download is heavy R&D, not for MVP). |
| BlizzTrack/ribbit-cli, Warpten/tactmon | open | active | Ribbit/TACT implementations if version metadata or (later) clientless download is ever pursued. |

## Login-gated verification checklist (interactive follow-up)

The client is currently open and responsive (`WINE_SIMULATE_WRITECOPY=1`, email pre-filled). To
finish verifying:
1. Complete login in the open window (prefix `~/feasibility-scratch/bnet/prefix`).
2. With the session live, `GET https://account.battle.net/api/games-and-subs` (reuse the client's
   cookies, or repeat the login in the Edge/CDP harness) → confirm `gameAccounts` lists your owned
   Blizzard titles.
3. Confirm `product.db` now enumerates any installed game via the parser.
4. `--exec="install <CODE>"` for one owned title → confirm the client starts the download; then
   `--exec="launch <CODE>"` → confirm it boots.
5. Restart the client once to observe self-update behavior (the churn risk in vivo).

## Sources

Lutris service: github.com/lutris/lutris `lutris/services/battlenet.py`, `lutris/util/battlenet/` ·
Playnite: github.com/JosefNemec/PlayniteExtensions `source/Libraries/BattleNetLibrary/`
(`Services/BattleNetAccountClient.cs` → `account.battle.net/api/games-and-subs`, `BattleNetGames.cs`) ·
product.db parser: github.com/TinkoLiu/blizzard-product-parser · Agent HTTP API + Ribbit/TACT:
wowdev.wiki/Agent, wowdev.wiki/Ribbit, wowdev.wiki/TACT · login white-screen fix
(`WINE_SIMULATE_WRITECOPY=1`, disable HW accel): github.com/GloriousEggroll/proton-ge-custom
issue #427, gamingonlinux.com 2023-12 · Ban stance: Blizzard 2025 Linux/Wine support statement ·
official API (no general ownership): develop.battle.net.
On-device artifacts: `~/feasibility-scratch/bnet/` (prefix with client + Agent 2.40.3.9700,
product.db, `logs/*.png` login screenshots incl. the responsive writecopy capture).

---

# Addendum: Phase 0 on-device spike (2026-08-09)

Executed against a **clone** of the retained prefix (`~/feasibility-scratch/bnet/prefix-clone-s1b`);
the original at `~/feasibility-scratch/bnet/prefix` was never written to. Implementation plan:
`~/.claude/plans/i-believe-we-performed-calm-sundae.md`.

**Corrections to the July report above. Where they conflict, this addendum wins.**

## 1. `PROTON_DISABLE_XALIA=1` was a placebo - the variable does not exist

The July report calls this "gating" and recommends pinning Proton because "Xalia is
Proton-version-coupled". Neither holds. `PROTON_DISABLE_XALIA` appears **nowhere** in the Proton
script. The real control is **`PROTON_USE_XALIA=0`** (or `noxalia` in compat_config), identical in
GE-Proton11-1 and GE-Proton11-3. During this spike `xalia.exe` ran the whole time despite the July
incantation being set, and the client was completely healthy: signed in, library rendered, no
"Blizzard Error". Treat the variable as precautionary, not gating, and drop the Proton-pinning
recommendation.

## 2. Launch is two-phase and `PROTON_VERB` decides whether it works at all

With the client already resident in the prefix:

| Verb | Result |
|---|---|
| `waitforexitandrun` | **Never reaches the exe.** Log stops after protonfixes, before `launching with umu.exe`. Blocked on `wineserver -w` waiting for the client's own wineserver. Command never lands. |
| `run` | Reaches the exe, command lands and the client acts on it. |

Phase A (starting the client) keeps `waitforexitandrun` because it owns the session; **Phase C must
use `PROTON_VERB=run`**. Also measured: the Phase C process does **not** exit promptly even on
success, so its return code carries no information - fire, wait briefly, then verify.

## 3. `Battle.net.exe` takes `--exec`, and there is no "Helper" process

`--exec` goes to `Battle.net.exe`, not `Battle.net Launcher.exe`. There is **no
`Battle.net Helper.exe` process**: that string is a command-line *argument*. Measured images:
`Battle.net.exe` (main, `--from-launcher`), `Battle.net.exe --type=renderer|gpu-process|utility`,
`Battle.net Launcher.exe`, `Agent.exe`. The client-ready signal is `--type=renderer` present, scoped
to the launching prefix; on a cold start renderers appeared ~5 s before the window.

## 4. Silent launch failure reproduced exactly

`--exec="launch D4"` (the obsolete Diablo IV family code) left the client **completely unchanged** -
no navigation, no error, no dialog, nothing in the log. `--exec="launch Fen"` navigated to Diablo IV.
Post-launch verification that a game process actually appeared is mandatory, not defensive.

## 5. The login survives a prefix clone

An rsync clone opened the client **fully signed in**, Online, with the owned library rendered
(All Games 34, My Games 4, Installed 0) and the friends list live. This is what makes the Ubisoft
three-tier model (auth prefix -> template -> per-game clone) viable for Battle.net. Clone cost:
**12 s for 1.6 GB / 4642 files**. `/home` is ext4, so **no reflink/CoW** - it is a real copy.

## 6. The template must be pre-warmed before it is cloned

The client self-updated **2.52.3.17554 -> 2.52.8.17651 within ~5 minutes** of first launch, wrote a
new sibling folder `Battle.net.17651` beside `Battle.net.17554`, bumped `product.db` field
`1.4.1.7`, and then raised a **blocking modal**: *"Battle.net Required Update - You need to restart
the application to finish installing a required update. This will pause any in-progress game updates
or installations."* Nobody can click that in Gaming Mode. Template build must therefore be: install
client -> launch once -> let it self-update -> restart -> verify healthy -> **then** snapshot. The
sibling-folder layout also confirms that "delete the newest version folder" is a viable repair.

## 7. `product.db` needs no protobuf dependency

A ~190-line schema-free wire walker parses it cleanly. Confirmed: field 1 = uid (`battle.net`),
field 2 = product_code (`bna`) - Lutris keys on field 2. `1.3.1` = install path, `1.3.2` = play
region, `1.4.1.1/2/3` = installed / playable / update_complete, `1.4.1.7` = version string.
Additions the July report did not have: **top-level fields `5` and `6` exist** beyond the repeated
product field and must be ignored gracefully, and **`1.4.4.2` is a float64 that read exactly `1.0`**,
which looks like a **download progress fraction**. If it tracks a live download, Battle.net can show
real percentage progress rather than an indeterminate bar - better than Ubisoft. Unconfirmed until a
real install is observed.

## 8. Miscellaneous measured facts

- umu rewrites `WINEPREFIX` to `<prefix>/pfx/` and creates `pfx -> .` as a **self-symlink**, so
  `<prefix>/drive_c` and `<prefix>/pfx/drive_c` are the same directory. Prefix-scoped process
  matching must resolve symlinks and normalise the trailing slash.
- `/proc/<pid>/environ` was readable for every live Wine process, so prefix scoping via environ is
  sound and stays outside the game's address space (anti-cheat hygiene).
- `WINEDLLOVERRIDES` merging works: our `locationapi=d` lands first, Proton appends its own defaults.
- The client exposes a per-title **"Handheld Optimized"** vs **"Unsupported"** flag (Diablo IV vs
  Hearthstone) - potential metadata to surface.
- CEF cookie jars: `common/Network/Cookies` holds exactly `bnet.pam`, `bnet.extra`, `web.id`; an
  account-scoped jar `<accountid>/Network/Cookies` holds `JSESSIONID`, `deviceTrackingId`,
  `sessionTrackingId` plus shop cookies. All `v10` AES-GCM. The `os_crypt.encrypted_key` is a **Wine
  placeholder DPAPI blob** (magic `DPAP`, provider GUID containing `Wine Crypt32 ok` three times,
  CALG_3DES + CALG_SHA1) with **no masterkey directory** in the prefix - a good prognosis for
  decrypting it offline, which would remove the second login.
- umu-launcher is now **1.4.4** (was 1.3.0 in July).

## Still open

- **S1**: the authenticated `games-and-subs` response body. The Edge harness renders the Battle.net
  login correctly and CDP is reachable on 9222; the fetch has not yet been completed.
- **S4 / S6-full**: process images during a live download, and whether `1.4.4.2` really tracks
  progress. Needs one real game install.

## 9. S1 closed: the ownership endpoint works, but not the way the July report assumed

`GET https://account.battle.net/api/games-and-subs` returned **HTTP 200** using session cookies
captured from the plugin's own Edge harness. The auth path is proven end to end. Three corrections:

**Cookie capture uses `Storage.getCookies`.** `Network.getAllCookies` is not available on the
browser-level target in modern CDP (Edge 150 returns `-32601 "wasn't found"`). `Storage.getCookies`
is the browser-level replacement and returns plaintext values including httpOnly - the reason to use
CDP at all rather than decrypt Edge's own `v10`-encrypted SQLite jar. 21 `.battle.net` cookies were
captured, including `bnet.pam`, `bnet.extra`, `web.id`, `SESSION`, `JSESSIONID`, `SESSIONID`,
`XSRF-TOKEN`, `BA-tassadar*` and `login.key`.

**The response is keyed by a numeric title id, so the catalog needs a THREE-way map.** Shape:

```
{"gameAccounts": [
  {"titleId": <int>, "localizedGameName": str, "gameAccountName": str|null,
   "gameAccountUniqueId": {"gameAccountId": int, "gameServiceRegionId": int, "programId": int},
   "gameAccountRegion": "US"|"KR"|"GLOBAL", "regionalGameFranchiseIconFilename": str,
   "gameAccountStatus": "Good"|"Trial", "lastPlayedDateMillis": int|null,
   "titleHasSubscriptions": bool, "titleHasGameTime": bool,
   "accountSubscriptionView": null, "gameTimeView": null,
   "displayOrder": int, "customDownloadLink": str|null} ]}
```

`titleId`/`programId` is neither the lowercase uid (`wow`, `hsb`, `pro`) nor the FAMILY code (`WoW`,
`WTCG`, `Pro`). Observed: WoW `5730135`, Hearthstone `1465140039`, Overwatch `5272175`, Diablo
Immortal `1095647827`, Call of Duty: Modern Warfare `1329875278`. The catalog entry must carry
**`title_id` alongside `uid` and `family`**. Playnite's `BattleNetGames.cs` "internal id" is very
likely this value.

**`gameAccounts` is "My Games", not a purchase ledger.** It returned **5 entries** while the client
UI on the same account showed "All Games 34 / My Games 4 / Favorites 21 / Xbox Game Pass 17". It
enumerates titles the account has a *game account* for - free-to-play titles the user has touched,
subscription titles, CoD - not everything purchased. `/api/classic-games` returned an empty list.

Enumerating what the account page itself calls (via `performance.getEntriesByType('resource')`)
found the adjacent endpoints, which were then probed:

| Endpoint | Contents |
|---|---|
| `/api/overview` | Account identity only (accountId, battleTag, email, country, balance). **No games.** |
| `/api/transactions?regionId=N` | **Region-scoped** purchase ledger + gift claims. Empty `purchases` for the region tested. |
| `/api/external-subs` | Linked external subscriptions - exposed the Xbox Game Pass link with a `licenseId` list. |
| `/api/details`, `/api/env`, `/api/age-verification`, `/api/time-gated-games`, `/api/location/*` | Account/profile plumbing, not ownership. |

**Open risk, not resolvable on this account:** whether a title that was *purchased but never
launched* appears in `gameAccounts`. This account has no such case (its `purchases` list is empty for
the region probed). If it does not appear, the library will under-report and `/api/transactions` must
be joined in across regions. Build the ownership layer so a second source can be merged without
restructuring it.

Useful bonus fields: **`lastPlayedDateMillis`** (real last-played timestamps from Blizzard),
`gameAccountStatus` (`Trial` must be surfaced or filtered - WoW came back `Trial`),
`gameAccountRegion` (also drives `play_region`), and `regionalGameFranchiseIconFilename` for artwork.

*Captured session cookies were shredded after use; only response shapes were retained.*

## 10. S4 partial: `--exec="install <FAMILY>"` does NOT start a download

This contradicts the Lutris-derived assumption carried by the July report and the implementation plan.
Measured on the current client (2.52.8.17651):

- `--exec="install ANBS"` and `--exec="install WTCG"` (a **known-good** family code, since
  `--exec="launch WTCG"` navigates to Hearthstone correctly) both left `product.db` unchanged,
  `aggregate.json` empty, and no new key under `Battle.net.config` `Games`. The client merely sits on
  the game page. The `install` verb fails as silently as an obsolete `launch` code.
- The install genuinely requires pressing **Install** in the client UI. (Blizzard may also show a
  handheld-compatibility interstitial for some titles; that is the user's to click through and is
  explicitly out of scope for this integration.)
- **xdotool cannot drive the client's UI.** Synthetic XTEST pointer events (activate + move +
  click, and separate press/release) did not reach the CEF renderer, though the pointer was
  confirmed over the button and inside the window. **Keyboard events DO reach it** (Escape dismissed
  the modal). So mouse automation is not available; `--exec` and keys are the only control surfaces.
- There **is** a local HTTP server reachable from native Linux on a randomised loopback port owned by
  wineserver (observed `127.0.0.1:22885`, `Server: Battle.net/2.52.8.17651`). It is alive but every
  guessed path returns 404, and it does not upgrade to websocket. Endpoint namespace unknown. Note
  this is the **client's** server, not the Agent's documented `:1120` (nothing listens there), and
  the client version in the `Server` header differs from the Agent version (2.40.3.9700).

### What this means for the design - it is not fatal

The Ubisoft precedent already covers this exactly. Ubisoft installs are *also* user-driven clicks
inside the vendor client's UI, with a watchdog polling for completion; that is precisely what
`download_phase="manual"` and `_ManualUiInstaller.install_via_upc_ui` exist for. So Phase 3 becomes:

1. ensure the per-game prefix,
2. `--exec="launch <FAMILY>"` to navigate the client to the right game page (**this half is proven**),
3. emit the install-launch event so the frontend `RunGame`s the client into a gamescope session,
4. the **user clicks Install** - genuinely manual, indeterminate progress, no synthetic percentage,
5. poll `product.db` for `installed AND playable AND update_complete`.

The optimistic auto-trigger is what fails; the navigate-then-hand-over model works and matches the
shipped Ubisoft flow. One consequence to design in: the randomised client HTTP port is worth
revisiting later as a possible real-progress source.

**Still unmeasured:** process images during a live download, and whether `product.db` field
`1.4.4.2` tracks download progress. Both need an install that actually runs.

## 11. S4/S6 measured against a live 12.43 GB Hearthstone install

Driven by a real user click (the `--exec` install verb does not work - see section 10). The client's
Download Manager reported 72% / 9.01 of 12.43 GB / 38.4 MB/s while the following was sampled.

### `aggregate.json` is the installed-state source, not `product.db`

`ProgramData/Battle.net/Agent/aggregate.json` is **plain JSON** and far richer than expected:

```json
{"installed": [{
  "product_id": "hsb",
  "name": "Hearthstone",
  "icon_path": "C:/Program Files (x86)/Hearthstone/Hearthstone Beta Launcher.exe",
  "launch_uri": "battlenet://game/hsb",
  "box_art_uri": "https://bnetxboxassets.akamaized.net/hsb/box-enUS.webp",
  "logo_art_uri": "https://bnetxboxassets.akamaized.net/hsb/logo-enUS.webp",
  "last_played_timestamp": 0,
  "icon_index": 0 }]}
```

This single file supplies **installed set, display name, the real game exe, a launch URI, official box
and logo art, and last-played** - no protobuf required. It should become the primary installed-state
and artwork source, demoting the hand-rolled `product.db` parser to a secondary role (completion flags
and version only). Two things it also reveals: `launch_uri` is `battlenet://game/<product_code>`, a
**third** launch form keyed on the lowercase code rather than the FAMILY, worth testing as a more
robust alternative to `--exec="launch <FAMILY>"`; and `box_art_uri`/`logo_art_uri` are official CDN
artwork that may reduce SteamGridDB dependence for Blizzard titles.

**Critical caveat: `aggregate.json` is written early.** The Hearthstone entry was present while the
download was still in progress (around 40%), so **presence does not mean installed**. Completion must
still come from `product.db`'s `installed AND playable AND update_complete`.

### `1.4.4.2` is NOT a download progress fraction - hypothesis withdrawn

It held at exactly `0.000000` across the entire download from 0.06 GB to 10.6 GB, as did `1.4.4.4`.
During the download `1.4.1.7` (version) is an empty struct. These fields only populate on completion,
so the earlier reading of `1.0` on the finished client was a completion value, not a progress bar.
**`product.db` carries no usable progress.** The growing on-disk byte count remains the only honest
signal, exactly as the Ubisoft-precedent design already assumed.

### `product.db` leaks the Agent API path form

Top-level field `3` of the in-progress record contained `'/install/hs_beta'` and `'/update/hs_beta'` -
the Agent HTTP API namespace, keyed on the **uid** (`hs_beta`), not the product code (`hsb`).

### The Agent HTTP API exists and is reachable from native Linux

While the download ran, a **second** wineserver-owned listener appeared. Probing it:

| | |
|---|---|
| Port | randomised (observed `127.0.0.1:45601`; the client's own server was `:22885`) |
| `/agent`, `/install/hs_beta`, `/update/hs_beta` | **HTTP 401** - the paths **exist**, they are auth-gated |
| Unknown paths on the client's `:22885` | 404 with `Server: Battle.net/<version>` |
| Documented port 1120 | nothing listening |

Auth was not solved in a time-boxed probe: Bearer / Basic(:session) / Basic(session:session) / raw /
`?session=` / `X-Session` using the `Agent.exe --session=<id>` value all returned 401, and the
response carries no `WWW-Authenticate` hint. **This is the highest-value post-MVP lead in the whole
study** - if the auth scheme is worked out, `/install/<uid>` would solve the install-trigger problem
and very likely expose the real progress the Download Manager displays.

### Other measured facts

- Install path is `C:/Program Files (x86)/Hearthstone`, i.e. **inside the prefix** - confirms
  "the prefix IS the install" and the destructive-reset guards.
- `product.db` uid was **`hs_beta`** while the product code was `hsb`. Field 1 is a variant string;
  **field 2 is the stable key**, confirming the Lutris choice. Keying on field 1 would have made
  Hearthstone unmatchable.
- Process images during a live download (the ground truth for `EXCLUDED_IMAGES`):
  `Battle.net.exe` x7 (incl. 3 `--type=renderer`, 1 `--type=gpu-process`, 2 `--type=utility`),
  `Battle.net Launcher.exe` x5, `Agent.exe`, plus Wine infrastructure (`winedevice`, `xalia`, `umu`,
  `tabtip`, `svchost`, `services`, `rpcss`, `plugplay`, `explorer`, `conhost`). Still no
  `Battle.net Helper.exe`.

### The completion transition, captured at 20 s resolution

```
t=620  hs_beta  hsb   installed=0 playable=0 update_complete=0   1.4.4.2=0.000000   1.4.4.4=0             11.29 GB on disk
t=640  hs_beta  hsb   installed=1 playable=1 update_complete=1   1.4.4.2=1.000000   1.4.4.4=12428894444   11.79 GB on disk
```

Three conclusions, all directly actionable:

1. **The three completion flags flip atomically in one write.** `installed`, `playable` and
   `update_complete` all go 0 to 1 in the same sample, so polling the conjunction is correct and there
   is no partial state to race against. The planned completion predicate is validated as-is.
2. **`1.4.4.2` is binary**, 0.0 during download and exactly 1.0 on completion. It is a completion
   value, not a progress fraction. Withdrawn as a progress source.
3. **`1.4.4.4` is the total install size in bytes.** `12,428,894,444` = **12.43 GB decimal**, matching
   the client's own Download Manager readout ("12.43 GB") exactly. It is `0` throughout the download
   and only populated at completion.

Point 3 has a sting: because the total is only written *after* the install finishes, it cannot drive a
percentage for a **first** install. It is usable for updates and re-installs (where a previous total is
already cached in the id_map), and it is a good cross-check on the completion signal. First-install
progress therefore stays indeterminate with a growing byte count, unless the Agent API auth is solved.

Verified end state: 12 GB in `drive_c/Program Files (x86)/Hearthstone`, `aggregate.json` naming
`Hearthstone Beta Launcher.exe` as the exe and `battlenet://game/hsb` as the launch URI.

## 12. Entitlements found - `games-and-subs` massively under-reports ownership

Section 9 flagged an open risk: `gameAccounts` returned 5 entries against a client UI showing far
more, and it was unclear where real ownership lived. **It lives in the client, locally, in plain
SQLite and plain JSON.** Both halves were located and joined successfully.

### Half 1: the account's licence list

`AppData/Local/Battle.net/CachedData.db` is a **plain SQLite** database. Table `key_value_store`, key
`features_cached_data_points`:

```json
{"account_country":"IND","account_id":309859116,"account_region":"US","geoip_country":"IN",
 "licenses":[168,236,260,263,274,16332,16515,17019,34998,43338,53736,107572,107743,
             601446,615331,931050,959845,1042650,1042653,1042668,1042675,1043667,
             1043668,1081728,1081786,1091697,1105059]}
```

**27 licence ids** - versus the 5 rows `games-and-subs` returned. The same table's `login_cache`
carries `battle_tag`, `account_id_hi/lo`, `environment` (`us.actual.battle.net`) and
`connected_environments` (`EU,KR,US,XX`), which is a clean account-identity source.

### Half 2: the PUB catalog maps licences to products

The client caches Blizzard's "PUB" catalog as **plain JSON fragments** under
`AppData/Local/Battle.net/Cache/**` (63 MB, content-addressed paths, no extension; `catalog_cache` in
`CachedData.db` records only `component`/`version`/hashes). Each fragment carries
`program_configuration`, whose rules are exactly a licence-to-product map:

```json
"program_configuration": {"ARK": {"run_each_rule": [
  {"match": {"license_id": [1105059, 1105074]},
   "actions": [{"add_product": {"product_id": {"id": "ARK", "type": "retail"}}}]}, ...]}}
```

### The join works

Scanning all cached fragments: **98 fragments parsed, 340 licence-to-product entries**, resolving
**14 of the 27 licences** to real products:

| family | type | family | type |
|---|---|---|---|
| `AQUA` | retail | `RTRO` | retail |
| `ARK` | retail | `W1R` | retail |
| `D1` | retail + `d1_hellfire` | `W2R` | retail |
| `D3X1` | ptr | `W3` | legacy + beta |
| `Fen` | beta | `WLBY` | retail |
| `LBRA` | retail + demo | | |

**None of these appear in `games-and-subs`** - Warcraft I and II Remastered, Diablo, Warcraft III,
Avowed, The Outer Worlds 2, Tony Hawk. That endpoint returns *game accounts* (titles with a service
account), which is a different and much smaller thing than entitlements. 13 licences stayed
unresolved only because the local cache holds a partial catalog; the mechanism is sound.

### Consequence for the design - ownership moves from web to client

This is a better source than the web endpoint on every axis: it is the ledger the client itself uses
to decide Install vs Buy, it needs no cookies and no CDP, it survives cookie expiry, and it yields
**family codes directly** - which also collapses the three-way id problem from section 9, since the
catalog carries the family and `aggregate.json` carries the product code.

Revised ownership design:

1. **Primary**: parse `CachedData.db` `licenses` x the cached PUB catalog fragments -> owned families.
   Both are plain SQLite/JSON, no protobuf, no auth, no network.
2. **Secondary**: `games-and-subs` over CDP cookies, for subscription and game-account state
   (`gameAccountStatus`, `lastPlayedDateMillis`, `gameAccountRegion`) that the licence list lacks.
3. **Bundled fallback**: the static ~42-title catalog, for cold start before the client has ever run.

The cost is that ownership now requires the client prefix to exist and have been signed into at least
once. Given the wrapper archetype already requires that for install and launch, this is not a real
constraint - but it does mean the library is empty until the user completes the client login, so the
auth flow should drive the client login first and treat the web login as optional enrichment.

Open: whether the client refreshes the licence list on every login (fine) or lazily (stale-ownership
risk), and how to trigger a full catalog fragment download rather than relying on what happens to be
cached.

## 13. The playable catalog is a rule engine, not a list

Section 12 framed ownership as "licences x catalog". That was too simple and
under-reported. Blizzard does not publish an owned-games list anywhere: each
catalog fragment carries a ``program_configuration`` whose **rules are evaluated
against account facts**, and the products those rules grant ARE the playable
library. Grammar enumerated from a real 254-fragment cache:

| match | count | | action | count |
|---|---|---|---|---|
| `license_id` (scalar or list) | 719 | | `add_product` | 1471 |
| `all_of` | 35 | | `add_tag` | 23 |
| `game_account` `{program_id}` | 29 | | `run_first_rule` (nested) | 6 |
| `any_of` | 21 | | | |
| `not` | 3 | | | |
| `flag` | 3 | | | |

**Free-to-play matches on `game_account`, not `license_id`.** Hearthstone's rule
is `match: {game_account: {program_id: "WTCG"}}` with an
`add_tag: {name: "play_for_free"}`. So the two account-fact sources are
**complementary, not competing** - and section 12's claim that `games-and-subs`
merely "under-reports" was wrong. It reports a *different dimension*, and it is
exactly what feeds the `game_account` rules. Licences alone gave 17 programs;
licences + game accounts gave **22**.

### Result: 100% of the playable catalog, verified

22/22 titles resolved to a real display name and install uid, zero unresolved:
Diablo Immortal, Avowed, Doom: The Dark Ages, The Outer Worlds 2, Call of Duty,
Diablo, Diablo II: Resurrected, Diablo III, Diablo IV, Tony Hawk's Pro Skater
3+4, Call of Duty: Modern Warfare, Overwatch, Blizzard Arcade Collection,
StarCraft, StarCraft II, Sea of Thieves, Warcraft I and II Remastered, Warcraft
III, Crash Bandicoot 4, Hearthstone, World of Warcraft.

Cross-validation: every `title_id` in the catalog matched the `games-and-subs`
titleId for the five game-account titles exactly (ODIN 1329875278, WTCG
1465140039, Pro 5272175, ANBS 1095647827, WoW 5730135).

### Catalog structure, and four traps that cost real time

`products[].base` is the metadata record: `program_id` (the `--exec` code),
`title_id`, `name` (a key into `strings`), `default_product_type`, `genre`,
`handheld_status`, and `types` mapping product type to uid.

1. **`types` is the uid map, not `installs`.** `types["retail"].uid` gave
   `hs_beta`, exactly the uid a real Hearthstone install used. `installs` lists
   every variant (WoW has 45, including `wow_ne_vendor11`).
2. **`types` is not always complete.** No cached WoW fragment carries a retail
   type, so a fallback to `installs` preferring the lowercased program id is
   needed - without it WoW resolves to `wow_alpha`.
3. **Fragments are partial and repeat**, so the type maps must be **unioned**
   across fragments, not first-wins.
4. **English is under locale `default`.** There is no `enUS` key at all.
5. **A granted product id is not always a program id.** Rules grant
   `ARIS_Standard`, `WoWPTR`, `D3X1`; those must fold into programs `ARIS`,
   `WoW`, `D3`. The granting program is known at evaluation time and must be
   recorded then - inferring it afterwards fails, because those ids are not
   catalog entries.

A sixth trap was caught by a unit test rather than by inspection: a rule's
`add_tag` actions are listed **after** `add_product` in the real catalog, so a
single ordered pass silently dropped `play_for_free` from every free-to-play
title. Action processing is two-pass for that reason.
