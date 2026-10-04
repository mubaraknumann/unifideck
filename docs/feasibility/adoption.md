# Feasibility Study: adopting games installed by other launchers

**Verdict: feasible, proven by a POC for Epic and Amazon.** A game another launcher (Heroic today)
installed for a store we already sync becomes installed in *our* Epic, GOG or Amazon store, in
place, without re-downloading. The store's own tool records it (`legendary import`, our GOG
`.unifideck-id` marker, nile's install record), and the existing shortcut flips to installed.

It is always started by the user; see [Always user-initiated](#always-user-initiated-decision-2026-09-25).
Games that match no store we sync are not adopted; they go to Local Vault ([vaults.md](vaults.md)).

## What users asked for

| Issue | 👍 | Ask |
|---|---|---|
| #97 | 1 (+2 "me too") | Point Unifideck at games already downloaded by Heroic or another launcher, including a shared Windows/SteamOS partition, to avoid re-downloading |
| #60 | 0 | Epic/GOG games installed by Heroic under `~/Games/Heroic/` show as not installed |
| #440 | 0 | Install GOG games another way, then import them |

## Discovery: one scanner, two outcomes

Adoption and Local Vault's installed-game import are the same disk scan ([vaults.md](vaults.md)). One scanner
(`stores/shared/discovery.py`) reads every input and decides, per game, who owns it. It only runs
when the user asks (see [Always user-initiated](#always-user-initiated-decision-2026-09-25)):

```
inputs                                   scanner                 outcome
Heroic installed.json (Epic/GOG/Amazon)  ->                      matches a store we sync?
goggame-<id>.info files                  ->  one list of          yes -> that store adopts it
folder of installed games                ->  "found N games"      no  -> Local Vault
folder of archives                       ->  shown to the user
Heroic sideload, Lutris (later)          ->
```

**Why a matched game must not go to Local Vault.**
- **Duplicates.** Our Epic store already lists that game as "not installed", so a Local Vault copy
  is a second, wrong shortcut.
- **It may not run.** Many Epic games will not start without legendary's launch (auth arguments,
  EOS).
- **Lost features.** Updates, cloud saves, achievements and playtime sync exist only in the owning store.

Adoption is cheap because the tools we bundle already do it:

| Store | How Heroic adopts | What we read | Gap today |
|---|---|---|---|
| Epic | `legendary import <app> <dir> --with-dlcs` | Heroic `legendaryConfig/legendary/installed.json` | we read only `~/.config/legendary/installed.json` (`stores/epic/legendary.py:44`) |
| GOG | `gogdl import <dir>` | Heroic `gog_store/installed.json`, or any `goggame-<id>.info` | bulk overlay keys only on our `.unifideck-id` marker (`stores/gog/library.py:290`); `~/Games/Heroic` is never scanned |
| Amazon | `nile import --path <dir> <id>` | Heroic `nile_config/nile/installed.json` | our nile config dir is separate |

Heroic roots: `~/.config/heroic/` and, for the Flatpak, `~/.var/app/com.heroicgameslauncher.hgl/config/heroic/`.

Each store supplies a row saying how it adopts a match.

### Always user-initiated (decision, 2026-09-25)

Nothing is discovered or adopted automatically. There is no background scan and no scan during a
sync; nothing changes until the user acts. Two ways in, both ending at the same adopt step:

**(a) The user asks for suggestions.** A "Find games from other launchers" action in settings runs
the scan, only then. It lists what it found and where each game would go ("Cassette Beasts
(Heroic) → Epic"). The user ticks games and presses Import. Unticked games are left alone and are
not suggested again automatically.

**(b) The user imports one specific game.**
- *From a tool:* "Import from Heroic" lists Heroic's installed games; the user picks one.
- *From the game:* a not-installed Epic, GOG or Amazon game's page offers "Already installed
  elsewhere? Locate folder", which opens a folder picker.

A hand-picked folder is checked before adopting:

| Store | Check |
|---|---|
| GOG | `goggame-<id>.info` in the folder names this game's id |
| Epic | `legendary import` compares the files with Epic's manifest and refuses a wrong folder |
| Amazon | weakest: bundled nile 1.1.2's import is broken (see [POC](#poc-results-2026-09-25)), so only `fuel.json` presence can be checked; a launcher's record, when present, gives the exact id |

After either path: the store's import, then `mark_installed` (flips the shortcut, writes the
`games.map` launch record, emits the event that refreshes the library tabs), and the old prefix is
recorded for the save copy. An adopted game becomes ours to update and uninstall, which is why the
user must choose it.

### What a suggestion scan reads

Used only by path (a), or by "Import from Heroic". Strongest evidence first; a game found by
several inputs is listed once.

| Input | Gives us | Covers |
|---|---|---|
| Heroic `installed.json` files (native and Flatpak roots, table above) | store id, install path, version, **and** the prefix via `GamesConfig/<appName>.json` → `winePrefix`, `wineVersion.type` | #60, most of #97 |
| Standalone legendary / gogdl / nile configs | store id, install path | already read for Epic today |
| Markers inside game folders, found by scanning the library roots we already scan (`utils/paths.py`, plus `~/Games/Heroic`, SD card, mounts) | GOG: `goggame-<id>.info`. Epic installed by the Windows Epic launcher: `.egstore/*.mancpn` (holds `AppName`) | #440, and #97's shared Windows/SteamOS partition, where no Linux launcher config exists |

A folder match alone is not enough to adopt. The id must also be in the user's library for that
store, because `legendary import` / `gogdl import` / `nile import` refuse games the account does
not own, and a mismatch must show as "not adoptable", not fail silently.

### The install folder: adopted in place

The `import` commands register the folder where it is; nothing is moved or copied. So the files
now belong to two launchers. If Heroic is still installed it may update the same folder, and an
uninstall in either one deletes the game for both. The adopt screen says so when the source was
Heroic. We do not edit Heroic's config.

### The prefix: new, never reused

We create our own per-game prefix as for every other game, and leave Heroic's untouched:
- **Its Wine may differ.** Heroic's prefix may be plain Wine, a different Proton, or a different
  prefix layout than ours.
- **Deleting it later would be dangerous.** We could never safely delete a prefix we did not create.
  The compatdata reclaim only trusts prefixes carrying our `.unifideck*` markers.

The cost is a first-launch setup (dependencies, redistributables), the same as a fresh install.

### Save files: copied forward, never moved

Saves live in one of four places, and only one needs work:

| Where | What happens |
|---|---|
| inside the game folder | nothing to do; it is adopted in place |
| Linux-native paths (`~/.local/share`, `~/.config`) for native builds | nothing to do; the same path is used |
| the store's cloud (Epic, GOG) | our existing cloud-save sync pulls them on first launch, if the user had cloud saves on in Heroic |
| **Heroic's prefix**, `drive_c/users/...` (`AppData`, `Documents`, `Saved Games`) | copy forward into our new prefix |

The copy reuses `launcher/proton/compat/save_migration.py`. Its `_merge_users` is already the
non-destructive, mtime-guarded merge used for the legacy umu-prefix migration: it never
overwrites a newer file, skips non-save subtrees, is safe to re-run, and runs once per prefix
behind a marker. The adopter records the Heroic prefix path; on our prefix's first creation it
becomes one more candidate source, next to `.save_backup` and the legacy umu prefixes.

One gap to close. `_merge_users` copies by relative path, but the Windows user folder differs:
- Proton prefixes use `drive_c/users/steamuser`.
- Heroic prefixes built with plain Wine use the Linux user name (for example `drive_c/users/deck`).

The Heroic source must map `users/<that name>/` onto `users/steamuser/` (and keep `users/Public`
as is), or the saves land where no game looks. `wineVersion.type` in the Heroic game config tells
us which case applies. Saves stored in the Windows registry are rare and not migrated.

Heroic's prefix is left intact. Because nothing is moved, a bad copy loses nothing, and the user
can still go back to Heroic.

## POC results (2026-09-25)

A standalone script (`~/feasibility-scratch/adoption-poc/adopt_poc.py`, no repo changes) adopted one
Heroic-installed game per store on the dev Deck (Heroic Flatpak 2.22.1), using only the plugin's
bundled tools, against the real Unifideck state.

| | Epic: Cassette Beasts | Amazon: Brothers | GOG: Dream Tactics |
|---|---|---|---|
| Registered in place | ✅ `legendary import --skip-dlcs` (reused the login, no refresh; flagged for verify on first update) | ✅ copied Heroic's nile row + manifest | ✅ `.unifideck-id` marker |
| Unifideck sees it installed after a sync | ✅ | ✅ (game page) | ❌ GOG scan never reaches `~/Games/Heroic/<Game>` |
| Launches | ✅ played 38 min; Epic cloud-save sync_down ran first | ✅ | not tried |

Findings the feature must handle:
- **Heroic keeps stale rows.** 13 rows, 3 folders on disk. Offer only folders that exist.
- **A sync flips install state but does not refresh the library tabs or write `games.map`.**
  Only `mark_installed` does both. Both games launched through the launcher's fallback exe
  resolution.
- **Bundled nile 1.1.2's `import` crashes** (`KeyError: 'downloadUrls'`, fixed upstream in 1.2.0).
  Running 1.2.0 once migrates the nile login to an encrypted format that 1.1.2 and our auth check
  cannot read, so a nile bump is not a drop-in swap. With 1.1.2, Amazon adoption copies the other
  launcher's nile row instead.
- **A game also owned on native Steam is hidden from the Unifideck tabs** by the live build's
  Steam-owned dedupe, even when only the Unifideck copy is installed (Brothers, Steam app 225080).
  Not adoption-specific.
- **`fuel.json` with trailing commas** fails our parser (`stores/amazon/amazon_fuel.py`). Not
  adoption-specific.
- None of the three Heroic prefixes existed (never launched in Heroic), so the save copy was not
  exercised.

## Recommended order

1. **Path (b) from a tool, and path (a)**, for Epic, GOG and Amazon: the Heroic reader, the
   per-store adopt rows, `mark_installed`, the GOG scan-root fix, and the recorded prefix for saves.
   Needs neither vault.
2. **"Locate folder" on a game's page** (path (b) from the game), with the per-store folder checks.
3. **Folder-marker scan** (`goggame-*.info`, `.egstore/*.mancpn`) for installs no launcher recorded.
4. **The Local Vault outcome** for games that match no synced store, once Local Vault exists.

## Risks

- **Two launchers own one folder.** An update or uninstall in either affects both. Warn at adopt time.
- **Amazon folder checks are weak** until nile is upgraded, and the upgrade migrates every user's
  nile login (see POC results).
- **A wrong exe** when a store gives none and the fallback finder guesses.
- **Saves in a plain-Wine Heroic prefix** need the `users/<name>` → `users/steamuser` mapping, not
  yet exercised on a real save.

## References

| Source | License | Use |
|---|---|---|
| HeroicGamesLauncher `storeManagers/*/constants.ts`, `games.ts` | GPL-3.0 | installed.json paths, import commands |
| imLinguin/nile `nile/utils/importer.py`, `nile/utils/config.py` | GPL-3.0 | import bug (1.1.2), auth migration (1.2.0) |
| kra-mo/cartridges `cartridges/importer/` | GPL-3.0 | multi-launcher importer reference |
