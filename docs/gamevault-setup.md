# GameVault setup

GameVault is a self-hosted library for the DRM-free games you own. You can use this integration to play DRM-free games that you have available locally or on a remote server.
 

| Where your games are | You Use | What you need |
|---|---|---|
| On a server somewhere else, and you want them on your Deck over the network | **A remote server** | A GameVault server that is running, and an account on it |
| Already on your Deck, an SD card, or a USB drive | **A folder on this device** | Nothing at all. No server, no account, no internet |

Only one can be active at a time. If you want to switch, just disconnect GameVault
and connect again with the other option.

To Sign In:

> Steam **⋯** button → **Decky** → **Unifideck** → **Settings** → **STORE CONNECTIONS**
> → **GameVault** → **Sign in**

That opens a window called **Connect to GameVault** with a button for each option.
Pick your part of the guide below and follow it from there.

---

# Part A: connect to a remote server

## Before you start

You will need a GameVault server that is already up and running. These are some links to get you started:

- [Server setup](https://gamevau.lt/docs/server-docs/setup/)
- [Using Docker Compose](https://gamevau.lt/docs/server-docs/setup/docker-compose/)

Once the server is running, you will need:

1. The address of your server. It uses port **8080** unless you changed it.
2. Your game files, sitting in the folder you mounted as `/files`.
3. A username and password. You create the first account through the server's own
   user management, not through Unifideck.

> **Tip:** open GameVault in a browser first and copy the address from the address
> bar.

## Step 1: Open the sign-in window

Go to **Settings → STORE CONNECTIONS → GameVault → Sign in**, then choose
**Connect to a remote server**.

## Step 2: Fill in your server details

**Server URL**


```
http://192.168.1.20:8080
https://games.example.com
```

**Username** and **Password**

The same account you used to sign in to GameVault.

**Verify SSL certificate**

Leave this switched on unless your server uses a certificate you made
yourself. For local servers this is can be left off.


**Archive download directory** (optional)

Unifideck uses `~/.local/share/unifideck/gamevault_downloads` by default. You can
set it to another directory if you are short on space. While a game installs, the
downloaded file and the unpacked game both exist for a moment, so the drive needs
room for both at once.

## Step 3: Connect

Press **Connect**. You should see a **GameVault connected** message, and a library
sync starts on its own.

## If it does not connect


| Message | What it means |
|---|---|
| Invalid username or password | Good news, your address is right, because the server answered. Check the account details. |
| Server returned HTTP 404 (or another number) | Something answered, but it was not GameVault. Check the port, and check for a stray path on the end. |
| Server response contained no token | The server answered but did not sign you in. Its own logs will say more. |
| Anything about SSL or a certificate | Turn **Verify SSL certificate** off and try again. |
| Cannot connect, connection refused, name not known | Either the address is wrong, the server is not running, or your Deck cannot reach it. Try opening the same address in the Deck's browser to find out which. |

---

# Part B: connect to a folder on this device

This one needs no server and no internet. You keep game archives in a folder, and
Unifideck reads that folder.

## Step 1: Open the sign-in window

Go to **Settings → STORE CONNECTIONS → GameVault → Sign in**, then choose
**Use a folder on this device**.

## Step 2: Choose your folder

Unifideck creates `~/Games/UnifideckVault` by default. To use your own folder/external drive,
press **Browse…** and pick it. 

Press **Connect**.

Unifideck creates the folder if it is not there yet and drops a `README.txt` inside
it for reference.

> **Note:** you will also find a hidden file named `.unifideck-vault` in the folder. This file is not to be touched.

## Step 3: Name your files

Name each archive after the game inside it. The is how the plugin looks up the game
metadata so make sure it is named as close to the real game title as possible.

```
Stardew Valley.zip
Hollow Knight.7z
```

> **Tip:** once a game is in your library, keep its filename as it is. Renaming an
> archive later creates a second, separate entry rather than updating the one you
> already have.

## Step 4: Know what gets picked up

These archive types are read:

`.zip` `.7z` `.rar` `.tar` `.tar.gz` `.tar.bz2` `.tar.xz` `.tar.zst` `.iso` `.wim`
`.cab`

A bare `.exe`, `.sh`, or AppImage is skipped, so pop it in an archive first.

You can keep the archive in the root or one folder deep.

```
~/Games/UnifideckVault/
    Stardew Valley (2016).zip
    Hollow Knight (2017).7z
    sd-card/
        Celeste (2018).zip
```

A game buried two folders deep will not show up.

---

# Installing and playing

From here it is the same whichever option you chose.

1. Go to **Settings → LIBRARY SYNC** and press **Sync Libraries**. A sync also runs
   by itself right after you connect, so you may find it already done.
2. Your games appear in your Steam library, and together under a **GameVault** tab.
3. Open a game and press **Install**. Unifideck asks where to put it, then shows you
   download and unpack progress. The default spot is `~/Games/GameVault`.
4. Press **Play**. Windows games go through Proton automatically, and Linux games
   run directly. There is nothing to set up either way.

> **Note:** if a game does not start, or an installer opens instead of the game,
> open its page in Steam, press the gear icon and choose **Change executable…** to
> pick the right file.

A few things that are nice to know:

- With a remote server, the downloaded file is cleaned up once the game is unpacked.
- With a local folder, your original archive is never touched or moved.
- Uninstalling removes only the installed game folder. Your archive stays put.

# Quick reference

| | |
|---|---|
| Default install location | `~/Games/GameVault` |
| Default local folder | `~/Games/UnifideckVault` |
| Default download staging | `~/.local/share/unifideck/gamevault_downloads` |
| Settings file | `~/.local/share/unifideck/gamevault_config.json` |
| Server default port | `8080` |
| Switching between the two options | Disconnect, then Sign in again and pick the other one |

