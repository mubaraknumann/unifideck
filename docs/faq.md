# Unifideck FAQ

This FAQ keeps to practical issues that have already shown up in release notes, code comments, or GitHub issues and comments. If you are troubleshooting an older install, update to the latest release first.

## Installation and setup

| Issue                                                                   | Resolution                                                                                                                                           |
| ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| The plugin install stalls around `installing plugin`.                   | Uninstall the current Unifideck plugin, then install the latest ZIP again. This was the recommended workaround during the 0.6.0 -> 0.6.1 transition. |
| Unifideck says a browser is required before sign-in or xCloud can work. | Install Microsoft Edge when prompted. Edge is the supported browser for shortcut-based auth and xCloud.                                              |
| I want installs somewhere other than internal storage.                  | Use **Storage Settings** to choose internal storage, SD card, or a validated custom path.                                                            |
| My new games are synced but do not appear in Steam yet.                 | Restart Steam when Unifideck prompts you. Sync and cleanup still need a Steam restart to fully refresh shortcuts and artwork.                        |
| I use multiple Steam accounts and old data is still hanging around.     | Use the account-switch prompt to migrate existing data or clear it cleanly instead of manually deleting shortcuts.                                   |

## Authentication and account connection

| Issue                                                                                 | Resolution                                                                                                                                                                               |
| ------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Epic login opens but the final page is blank or shows `Pretty Print`.                 | Sign into Epic in a normal browser first, accept any pending legal updates, then retry in Unifideck.                                                                                     |
| Microsoft sign-in completes but xCloud still does not start cleanly on the first try. | After the first successful sign-in, open xCloud once and click **Play via Cloud** inside the Microsoft Cloud Gaming home screen to finish OAuth.                                         |
| Microsoft or Ubisoft auth times out unexpectedly.                                     | A full SteamOS reboot is a good first retry. One issue thread also reported better Ubisoft auth behavior with GE-Proton-10-23 than Proton Hotfix or Experimental.                        |
| Ubisoft games bought through Epic hang on login or ask for a key.                     | Link your Epic and Ubisoft accounts once at `epicgames.com/id/link/ubisoft`, then retry the launch.                                                                                      |
| Sign-in loops or keeps failing after updates, especially with Ubisoft.                | Clear `~/.local/share/unifideck/chromium-auth`, `~/.local/share/unifideck/ubisoft_installer_cache`, and the Ubisoft prefixes under `~/.local/share/unifideck/prefixes/`, then try again. |

## Library sync, artwork, and display

| Issue                                                                                  | Resolution                                                                                                                                               |
| -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| All artwork is missing after sync.                                                     | Run **Force Sync** from Library Sync and restart Steam. That resolved issue #222, and later releases also shipped artwork sync fixes.                    |
| Cover art disappeared after an older 0.5.x build.                                      | Update to the latest release and force-sync artwork again. The artwork sync path was fixed in 0.5.3 and the query/filtering was improved again in 0.6.0. |
| The custom Install / Play area is too low on the page or I see duplicate play buttons. | Update to at least 0.5.5. The public fix covered the language-sensitive native play button hiding logic and game-details placement issues.               |
| Great on Deck / richer metadata does not show up right away.                           | Sync or force-sync once, then restart Steam so the richer metadata can be loaded into the library view.                                                  |
| All libraries show `0` games even after a sync.                                        | Update to at least 0.4.2 and sync again. That release explicitly fixed the all-zero library state for affected users.                                    |
| TabMaster is installed and I do not see Unifideck's custom tabs.                       | This is expected. Unifideck skips custom tab injection when TabMaster is present and expects you to use `[Unifideck]` collections instead.               |

## Downloads, updates, and launch behavior

| Issue                                                                    | Resolution                                                                                                                                             |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Launch options keep resetting.                                           | Environment-variable options in the **Launch Options** field survive a normal sync, but a **Force Sync** resets the shortcut to its default — re-add them afterward. See [Launch Options → persistence](launch-options.md#persistence-across-library-sync).             |
| A game won't launch, or shows "Path Not Found".                          | Delete the game's prefix (`rm -rf ~/.local/share/unifideck/prefixes/<game_id>`) and relaunch — the launcher rebuilds it. See [Proton → Troubleshooting](proton-compatibility.md#troubleshooting--quick-fixes).                                                          |
| LSFG does not activate.                                                  | Wrapper-style launch options, including `~/lsfg` / `LSFG=1`, are **not currently wired into the launcher** (a regression from the 0.6.0-era support). See [Launch Options → Planned](launch-options.md#planned--not-yet-wired).                                          |
| Ubisoft games uninstall themselves after I change Proton.                | Choose the Proton version before installing. Changing Proton after install can invalidate the prefix and force a redownload.                           |
| A GOG DOSBox game launches the wrong executable.                         | Update to 0.6.0 or newer. Generic DOSBox fixes were added there after reports like issue #248.                                                         |
| Epic or GOG DLC is missing after install.                                | Use a build from 0.6.0 or newer and rerun the install or update path. Owned DLCs are downloaded automatically there.                                   |
| The download / update progress UI is missing from the game details page. | Update to at least 0.5.5. That release added the progress tracker and update-status integration to the custom play section.                            |
| A GOG game stopped launching after an update.                            | Update to at least 0.5.6. That release specifically fixed GOG launch regressions.                                                                      |
| Large GOG downloads cancel or never finish properly on old builds.       | Update to at least 0.4.0, which fixed large multi-part GOG downloads being canceled before completion.                                                 |
| Game updates are not detected consistently.                              | Update to at least 0.5.5. Later builds added explicit update checks and update progress wiring for the play section.                                   |

## Store-specific behavior

| Issue                                                                       | Resolution                                                                                                                                                                                 |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A GOG game never asked me for a language.                                   | The language modal only appears for games that actually expose multiple supported languages. The feature was added in 0.5.4.                                                               |
| Epic titles that need Ubisoft / Uplay prerequisites still fail.             | Update to at least 0.5.5 and make sure the Epic and Ubisoft accounts are linked. That release added the automatic prerequisite / login path for Epic games that depend on Ubisoft Connect. |
| Online GOG titles such as Gwent do not connect through GOG Galaxy features. | Update to at least 0.5.2, which added Comet / GOG Galaxy support for compatible titles.                                                                                                    |
| Hidden games keep reappearing in older builds.                              | Update to at least 0.5.0. Hidden games were explicitly added there and hidden Steam games were kept hidden as well.                                                                        |
| Amazon support is missing entirely.                                         | Amazon support first shipped in 0.4.0. If you are on an older package, update.                                                                                                             |
| Cloud saves do not work for every store or every game.                      | That is expected. Cloud save support currently targets Epic and GOG, and support still depends on the individual game.                                                                     |
| An itch.io bundle game is missing from my library. | Games from a bundle only appear once you claim them on itch.io (open the bundle page and press "Download" or "Claim" for the game). Unifideck lists what you own or have claimed, plus the free games in your collections. |
| A paid game in my itch.io collection is not listed. | Collections are bookmarks, so a paid game appears only once you have bought it. Free collection games are listed and install like owned ones. |
| itch.io asked for my password twice when connecting. | That is itch.io's own check before it shows your API keys. If no key is listed, press "Generate new API key" once; Unifideck reads the key from the page. |
| An itch.io game says "Play in Browser" instead of Install. | The game only has a browser (HTML5) version. It opens in an Edge window, like Xbox Cloud Gaming, and needs a network connection. |
| I disconnected itch.io but the API key is still on my account. | Disconnecting only removes the key from this device. Revoke it at itch.io → Settings → API keys if you no longer want it. |

## Logs and debugging

| Issue                                    | Resolution                                                                                                                                               |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| I was asked for my logs.                 | Open the Unifideck Quick Access panel, go to **Settings -> Capture Logs**, and confirm. Every log and state file is collected into one zip in your **Downloads** folder, along with a report of your device and where everything lives. Attach that zip. Passwords, store login tokens and browser cookies are never included. |
| I need the main plugin / backend log.    | `~/homebrew/logs/Unifideck/` holds one file per plugin session.                                                                                          |
| I need launcher or xCloud-specific logs. | Use `~/.local/share/unifideck/launches/` for launches and installs (`<id>.log` is the plugin side, `<id>.game.log` is Proton/game output), and `~/.local/share/unifideck/edge-auth.log` for Edge / xCloud auth behavior. |
