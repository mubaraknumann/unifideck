# Store-expansion feasibility studies

Round 1 (2026-07-03): EA App, Battle.net, itch.io. Round 2 (2026-09-24): library sources, Rockstar, Humble, plus an evaluation of Xodus.

Round 1 was a set of independent feasibility studies for adding three new stores, commissioned to plan
future development. Each combines online research against maintained primary sources with
on-device prototyping on a Steam Deck (SteamOS Desktop Mode, 2026-07-03). Full detail per store:
[itch.md](itch.md) · [ea.md](ea.md) · [battlenet.md](battlenet.md).

**MVP feasibility bar (user-defined):** login → enumerate owned games → sync into Steam →
download/install → launch. Cloud saves / achievements / playtime-sync are stretch, not gating
(matches the Amazon/Ubisoft precedent, which ship without them).


## Round 2 (2026-09-24, v0.7.6): after itch.io and EA

itch.io shipped in 0.7.6 and EA is being built separately. This round looked at what users ask
for (every GitHub issue) and what comparable tools support (Heroic, Lutris, Playnite, GOG Galaxy
plugins, NonSteamLaunchers, Junk Store). Full detail:
[adoption.md](adoption.md) · [vaults.md](vaults.md) · [rockstar.md](rockstar.md) · [humble.md](humble.md) · [xodus.md](xodus.md).

| | Adoption | Local Vault + Remote Vault | Rockstar | Humble |
|---|---|---|---|---|
| **Verdict** | ✅ Proven by POC for Epic and Amazon; GOG needs a scan-root fix | ✅ Feasible, mostly existing code | ⚠️ Feasible for single-player; ownership unmeasured | ✅ Feasible, moderate fragility |
| **User demand** | #97, #60, #440 | #29 (5 👍), the top open store request | 3 bug reports from Epic-Rockstar users | None in our tracker |
| **Competitors** | Heroic (`import` via the same CLIs) | RomM: Tender, Ludo (ROMs only) | Playnite (official), Galaxy (stale) | Lutris, Playnite, Galaxy, NSL, Heroic PR |
| **Type** | Per-store adopt rows over the existing Epic/GOG/Amazon stores; always user-initiated | Two stores over one shared pipeline, from GameVault | Wrapper (Battle.net template) | API store; reuses the vault install pipeline |
| **Sign-in** | none new | RomM device code / pairing; file shares by URL + credentials | Inside the client window | Standard Edge window, HttpOnly cookie over CDP |
| **Top risk** | Two launchers own one folder; Amazon checks weak on nile 1.1.2 | Splitting a shipped store (local-mode games must keep their app ids) | Ownership read from a log file; self-updating client | Unofficial API behind Cloudflare |
| **On-device result** | POC: Epic and Amazon adopted and launched | Not needed yet (design study) | Client installed unattended (`/quiet /norestart`) and rendered under GE-Proton11-7 | Anonymous API probe only |

**Recommended order:**

1. **Adoption** for Epic, GOG and Amazon, started by the user (suggestions, or one game from
   Heroic). Most requested, no refactor, proven by the POC.
2. **Split GameVault into Local Vault and Remote Vault** over a shared pipeline, migrating 0.7.5
   local-mode games with their old app ids. Then RomM and FTP/HTTP/WebDAV shares in Remote Vault,
   and installed-game folders in Local Vault.
3. **Humble**, on top of that pipeline, once the run-the-installer step exists.
4. **Rockstar**, only after a signed-in spike proves ownership can be read.

Watch, don't build: [Xodus](xodus.md) (native Xbox PC / Game Pass on Linux). It is pre-alpha with
no releases, and it runs games only on its own Wine fork. We would integrate it as an external tool
in the existing Microsoft store once it ships a release, never fork it. Also watch Drop (pre-1.0). Skip: Riot (Vanguard), Legacy Games, IndieGala, Big Fish (no API, no requests).
Cheap extra: Luna, GeForce NOW and Boosteroid as browser games (`GameTag.BROWSER`), no store code.

*Round 2 written 2026-09-24 against v0.7.6. Uncommitted per repo policy.*

## Round 1 verdict matrix

| | itch.io | EA App | Battle.net |
|---|---|---|---|
| **Verdict** | ✅ Feasible now | ✅ Feasible | ⚠️ Feasible with caveats |
| **Archetype** | CLI (Amazon/nile template) | Launcher-wrapper (Ubisoft template) | Launcher-wrapper (Ubisoft template) |
| **Login** | Paste API key — headless, no browser | Web OAuth via Edge/CDP (existing harness) | Client login window (needs Wine env fix) |
| **Owned-games enumeration** | `Fetch.ProfileOwnedKeys` / owned-keys API | juno GraphQL `ownedGameProducts` (owned, server-side) | `account.battle.net/api/games-and-subs` + static ~42-title catalog |
| **Install/launch** | butlerd `Install.*`; native or umu | `origin2://` deeplink, client-driven, watchdog | `--exec="install/launch <code>"`, client-driven, watchdog |
| **New launcher handler** | Windows: none (generic); Linux: small native branch | Yes (`ea.py`, deeplink+watchdog) | Yes (`battlenet.py`, deeplink+watchdog) |
| **unifiDB catalog work** | `external_ids["itch"]`; weak IGDB coverage (indie long-tail) | `external_ids["ea"]`; good coverage | `external_ids["battlenet"]`; excellent coverage, tiny catalog |
| **Effort vs yardsticks** | ~Amazon (≈8–10 files, ~2k LOC) | Ubisoft-lite (≈15–25 files, ~4–6k LOC) | ~Ubisoft (≈20–30 files, ~5–8k LOC) |
| **Top risk** | Soft: API rate-limits (429), bundle-claim UX | EA App self-update breakage under Wine | Client self-update breakage + login white-screen |
| **On-device result** | butler runs on SteamOS; butlerd handshake OK; API endpoint alive | Installer completed unattended; login rendered; **user logged in**; IS file populated | Installer + client OK; **full login E2E: library window rendered with owned games ("My Games", 33 titles)**; requires `WINE_SIMULATE_WRITECOPY=1` + `PROTON_DISABLE_XALIA=1` |

Yardsticks (from the current codebase): Amazon/nile ≈ 7 files / 1.5k LOC (CLI floor); Ubisoft/UPC
≈ 50 files / 12k LOC (wrapper ceiling). Cloud saves, playtime-push, and achievements are opt-in
subsystems (Epic/GOG only today) — none of the three needs them for MVP.

## Recommended sequencing

1. **itch.io first.** Lowest risk and effort, closest to an existing store (Amazon/nile), and the
   auth story is uniquely simple (paste an API key — no Edge, no CDP, no temp shortcut). All four
   MVP steps are documentation-proven and the tooling was exercised on-device. Already tracked as
   issue #136; the frontend already reserves the `"itch"` id. The only novel piece is running
   butler as a long-lived JSON-RPC daemon rather than a one-shot CLI (~100 lines), plus a
   native-Linux launch branch (itch is our first store with native Linux builds). Ships a whole
   store for roughly the cost of a store-and-a-half of nile.

2. **EA App second.** Higher effort (wrapper), but the strongest evidence base: the installer ran
   unattended, the login window renders, and the user completed a real login on-device — the two
   historical blockers (blank WebView2, ownership enumeration) are respectively fixed and solved.
   It's a *cheaper* wrapper than Ubisoft because ownership comes from a real server-side API
   (juno GraphQL) instead of binary parsing + an Algolia UUID bridge. Reuse
   `docs/ubisoft-store-spec.md` structure and the `stores/ubisoft/` prefix/watchdog patterns.

3. **Battle.net last.** Feasible and well-understood (Lutris + Playnite both ship maintained
   services to model from, and the owned-games endpoint is clean), but the finickiest to keep
   working: the client self-updates aggressively and the login needs a mandatory Wine workaround
   (`WINE_SIMULATE_WRITECOPY=1` + disable browser HW accel — confirmed on-device). Effort is
   Ubisoft-scale. Do it once the wrapper machinery from EA App exists to share.

## Cross-cutting observations

- **Two wrappers can share machinery.** EA App and Battle.net are both Ubisoft-shaped: per-game
  prefix hosting a vendor client, deeplink-driven installs with indeterminate progress, a
  resident-client watchdog, and auth capture-back. Building EA App second means the third store
  mostly reuses that scaffolding. Worth factoring the Ubisoft-specific wrapper bits into a shared
  base before adding the second wrapper (roadmap #10's parametrized store-contract harness would
  help lock this down).
- **Auth spans the full range.** itch = paste-a-key (simplest we'd have); EA = standard Edge/CDP
  OAuth capture (identical to Epic/GOG/Amazon); Battle.net = client-window login whose session
  cookies also unlock the ownership endpoint.
- **No open CLI for EA/Battle.net.** Heroic marked EA support wontfix over the undocumented
  download protocol; there is no Blizzard ownership CLI. The wrapper is the only path for both —
  confirming the Ubisoft archetype is the right template, not a new one.
- **Licensing hygiene.** The best references (Lutris GPL-3.0; the EA Galaxy fork has no license)
  can't be copied wholesale — reuse endpoints, data shapes, and protocol facts (not copyrightable)
  and re-derive code, exactly as the Ubisoft integration did from Lutris.
- **Frontend is ready.** `StoreIdExtended` already reserves `ea`/`battlenet`/`itch`, support URLs
  are wired, and react-icons ships `SiEa`/`SiBattledotnet`/`SiItchdotio`. Promoting each into the
  base `StoreId` union + `STORE_VISUALS`/`STORE_ICONS` is small, known work.

## On-device evidence

All prototypes ran in `~/feasibility-scratch/{itch,ea,bnet}/` (home partition; never touched
`~/.local/share/unifideck`, user prefixes, Steam data, or `~/homebrew`). Screenshots and logs are
under each store's `logs/`. Retention: the EA and Battle.net prefixes and the butler binary are
kept for the interactive follow-up checks listed in each report; teardown is a single
`rm -rf ~/feasibility-scratch` (after `wineserver -k` per prefix), freeing ~4 GB.

*Reports written 2026-07-03 against v0.7.0. Uncommitted per repo policy.*
