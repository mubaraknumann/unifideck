# Feasibility Study: Rockstar Games Launcher

**Verdict: feasible for single-player, with one open question.** Rockstar is a wrapper store,
like Battle.net and Ubisoft. The launcher installs unattended and renders under GE-Proton11-7
on this Deck (measured 2026-09-24). Install and launch have documented command-line arguments.
The open question is ownership: Rockstar has no clean owned-games API, so the library has to be
read from the launcher's own log after the user signs in. That step needs a real account and
has not been measured yet.

Online play is out of scope for good. GTA Online has used BattlEye since 2024-09-17 and Rockstar
does not allow it on Linux. Red Dead Online reports are mixed.

User evidence: three GitHub issues from people running Rockstar titles bought on Epic
(#193 RDR2, #267 and #322 GTA V). Playnite ships an official Rockstar library; GOG Galaxy has a
community plugin (unmaintained since 2022).

## What we already have

Rockstar titles bought on **Epic** already run through the Rockstar launcher.
`launcher/proton/compat/rockstar_egs.py` drops Heroic's fake `EpicGamesLauncher.exe`, writes a
`.bat` shim that starts the game through it, and registers the `com.epicgames.launcher`
protocol. `launcher/proton/fixes/game_fixes.py` holds the `ROCKSTAR_*` tables (app names, play
exes, `WINEDLLOVERRIDES=vulkan-1=n,b`). The #267 reporter confirmed that fix works in v0.7.2.
Their remaining complaint was that an update rebuilt the prefix and forced a new Rockstar
sign-in. A Rockstar store would need the same sign-in persistence, so that work helps both.

## MVP walkthrough

| Step | Status | Evidence |
|---|---|---|
| Install the client | **Measured** | WiX Burn bundle. `Rockstar-Games-Launcher.exe /quiet /norestart` under umu 1.4.4 + GE-Proton11-7 installed it unattended into a fresh prefix and wrote `HKLM\...\Wow6432Node\...\Uninstall\Rockstar Games Launcher`. |
| Client renders | **Measured** | Main window drew fully (store pages, art, sidebar). A separate "Rockstar Games - Sign In" window (700x800) opened. No blank web view, no Proton tweaks beyond `PROTON_USE_XALIA=0`. |
| Sign in | Not measured | Sign-in happens inside the client window, as Battle.net does. Needs the user's account. |
| Owned games | **Open question** | No owned-games API. Two known routes, below. |
| Installed games | Proven (docs) | Uninstall keys whose `UninstallString` ends `uninstall=<titleId>` (Playnite's method), read from the prefix's `system.reg`. |
| Install a game | Proven (docs) | `Launcher.exe -enableFullMode -install=<titleId>` (Galaxy plugin). Uninstall: `-uninstall=<titleId>`. |
| Launch | Proven (docs) | `Launcher.exe -launchTitleInFolder "<InstallDir>"` (Playnite). The launcher is the DRM, so it must be running; games cannot start without it. |

### Ownership: the open question

- **Log parse (realistic).** After sign-in the launcher writes `Documents\Rockstar Games\Launcher\launcher.log`,
  rotated to `launcher.01.log` and so on. The Galaxy plugin reads `on branch` lines as owned and
  `no branches!` as not owned. Our unsigned-in run already logs `[titlemanager] Downloading title list`
  and `Parsed response from GetDefaultApps: 1 title(s), 1 branch(es)`. A signed-in run should list
  every entitled title the same way. **This is the one thing to measure next.**
- **Social Club web scrape (fragile).** The Galaxy plugin read `gamesOwned` from
  `socialclub.rockstargames.com/ajax/getGoogleTagManagerSetupData`. That is a *played* list, it
  sits behind anti-bot cookies and fingerprinting, and the plugin has not been updated since 2022.
  Do not build on it.

The title catalog itself is public and needs no auth:
`gamedownloads-rockstargames-com.akamaized.net/public/title_metadata.json` (titleIds `gta5`,
`rdr2`, `lanoire`, `mp3`, `gtasa`, `gta3`, `gtavc`, `bully`, `gta4`, the `*unreal` trilogy, and so
on, each with its registry key, main exe and prerequisites). It is missing `gta5_gen9` (GTA V
Enhanced) and `rdr` (Red Dead Redemption 1), which Playnite adds by hand. We would keep a small
table like Battle.net's.

## Measured on-device (2026-09-24)

Scratch: `~/feasibility-scratch/rockstar/` (prefix, installer, logs, screenshots; 1.2 GB).
Never touched `~/.local/share/unifideck`, Steam data or the live plugin.

- Installer: `https://gamedownloads.rockstargames.com/public/installer/Rockstar-Games-Launcher.exe`,
  111,419,512 bytes, sha256 `321886457b7e…3746`. It is replaced in place (Last-Modified 2026-09-03),
  so never pin its hash.
- Client version 1.0.109.3031. It self-updates (`downloadAndSwap: true` for `launcher` in
  `title_metadata.json`).
- The installer does not exit on its own. It starts `Launcher.exe -upgrade` at the end and waits,
  so an install step must watch for `Launcher.exe` (or the Uninstall key) and move on, as the
  Battle.net install does.
- Harmless errors on every start: `No version info for socialclub.dll`,
  `Folder permissions operation failed`, `Could not do OpenProcess on parent process`.

## Integration design

Wrapper archetype. Follow the drift-guard "wrapper store" row: `WRAPPER_STORES`, a decision on
`_PREFIX_OWNS_INSTALL` and `_SKIPS_GENERIC_COMPAT`, `wrapper_prefix_probe._SPECS`,
`prefix_bridge.resolve_prefix`, `_wrapper_handler`, `_STORE_LAUNCHERS`, `CLIENT_STOREFRONTS`,
`AuthDispatcher`, `VENDOR_LOG_GLOBS` (add `launcher*.log`) and a `preserve_vendor_logs` call.

- **Session.** Use the shared `launcher/wrapper_session.py` path Battle.net already uses. The
  material to carry across prefixes is `Documents\Rockstar Games\Social Club\` (the #267 users
  had to copy this folder from Windows to get past "session hasn't started"). Measure which files
  hold the sign-in before building.
- **Epic overlap.** A title owned on both Epic and Rockstar must appear once. Rockstar-on-Epic
  games already work, so dedupe toward the Epic entry and let the Rockstar store own only
  Rockstar-bought titles. `rockstar_egs.py` stays as it is.
- **Anti-cheat gate.** Label GTA V and RDR2 "story mode only", as Heroic does.

## Risks

- **Ownership rests on a log format.** Rockstar can change it in any client update.
- **Self-updating client.** Same class of risk as Battle.net; a new client version can break the
  install flow or the log format.
- **Sign-in loss on prefix rebuild** (seen in #267). Must be solved before shipping, not after.
- **Terms of service.** The EULA forbids reverse engineering and third-party software that
  modifies games. Driving the official client with its own documented arguments is the lower-risk
  path; scraping Social Club is not.

## Next step

Sign in to the scratch launcher with a real Rockstar account, then check that:
1. the log lists owned titles;
2. `-install=<titleId>` starts a download;
3. copying `Social Club\` into a second prefix keeps the sign-in.

Those three answers decide whether this is a go.

## References

| Source | License | Use |
|---|---|---|
| JosefNemec/PlayniteExtensions `RockstarLibrary` | MIT | installed detection, launch args |
| tylerbrawl/Galaxy-Plugin-Rockstar | MIT | install/uninstall args, log-based ownership |
| moraroy/NonSteamLaunchers `NonSteamLaunchers.sh` | not checked | installer URL, vcredist step |
| Heroic wiki "Rockstar Games from Epic Games" | GPL-3.0 project | Epic handoff (already implemented) |
| Rockstar `title_metadata.json` | proprietary data | read at runtime, do not redistribute |
| AreWeAntiCheatYet, ProtonDB | public | GTA V online denied; RDR2/GTA V gold, RDR1 platinum |
