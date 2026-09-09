# Launch Options Guide

How to pass extra options (performance overlays, Proton/Wine tweaks, frame generation) to a
Unifideck game.

Every Unifideck shortcut's launch options start with a `store:game_id` token, e.g.
`gog:1207658924`. **Never remove it.** It is how the launcher knows which game to start. Add
your own settings around it.

To edit: Steam **Library** → right-click the game → **Properties** → **Shortcut** →
**Launch Options**.

---

## Setting environment variables

Put `VAR=value` **after** the game id:

```
gog:1207658924 MANGOHUD=1
gog:1207658924 MANGOHUD=1 DXVK_HUD=fps
gog:1207658924 WINEDLLOVERRIDES="dxgi=n,b"
```

Quote values containing spaces. Your value takes priority over the one Unifideck would pick
for that game, so change one thing at a time.

> [!IMPORTANT]
> Put variables **after** the game id, not before `%command%`. The `VAR=value %command%` form
> works for ordinary Steam games but is unreliable for Unifideck games, where the variable
> can be dropped before the game starts.

### Useful variables

| Variable | Effect |
| --- | --- |
| `MANGOHUD=1` | Enable the MangoHud performance overlay |
| `MANGOHUD_CONFIG=fps_limit=60,...` | Configure MangoHud |
| `DXVK_HUD=fps,frametime` | DXVK's built-in stats overlay |
| `DXVK_FRAME_RATE=60` | Cap the frame rate via DXVK |
| `PROTON_USE_WINED3D=1` | Use OpenGL (WineD3D) instead of DXVK/VKD3D |
| `PROTON_NO_ESYNC=1` / `PROTON_NO_FSYNC=1` | Disable esync/fsync (workaround for some games) |
| `PROTON_ENABLE_NVAPI=1` | Enable NVAPI emulation |
| `WINEDLLOVERRIDES="dxgi=n,b"` | Override specific Wine DLLs |

### Variables you cannot set

The launcher manages these and overwrites whatever you provide:

```
PROTONPATH  WINEPREFIX  STEAM_COMPAT_DATA_PATH  STEAM_COMPAT_INSTALL_PATH
GAMEID  STORE  PROTON_VERB  DXVK_NVAPI_ALLOW_OTHER_DRIVERS
```

`UNIFIDECK_*` variables are Unifideck's own and are removed from game shortcuts on sync.

---

## Frame generation (LSFG)

Add either flag after the game id:

```
gog:1207658924 LSFG=1
gog:1207658924 ENABLE_LSFG=1
```

Unifideck reads your `~/lsfg` profile and applies it to the game. Requires Lossless Scaling
(Steam) plus the Decky LSFG-VK plugin. With that plugin not installed, the flag does nothing.

---

## Wrapper programs

Wrappers go **before** `%command%`, with the game id after it:

```
mangohud %command% gog:1207658924
gamemoderun %command% gog:1207658924
```

Steam applies the wrapper, so this works. A wrapper name written after the game id is
ignored.

Wrappers and variables can be combined:

```
mangohud %command% gog:1207658924 LSFG=1
```

---

## Choosing a Proton version

Proton is **not** selected via launch options. Use the game's **Properties → Compatibility**
and tick *"Force the use of a specific Steam Play compatibility tool"*. Unifideck detects your
choice. The default is the latest GE-Proton. See
**[Proton Compatibility](proton-compatibility.md)**.

---

## Reference

`<id>` is your game's `store:game_id` token.

| Launch options | Effect |
| --- | --- |
| `<id>` | Normal launch (the default) |
| `<id> VAR=value` | Sets `VAR` for the game |
| `<id> VAR=value VAR2=value2` | Sets both |
| `<id> VAR="two words"` | Quoted values are kept whole |
| `<id> LSFG=1` | Frame generation on |
| `<id> ENABLE_LSFG=1` | Same as `LSFG=1` |
| `<id> LSFG=1 MANGOHUD=1` | Frame generation plus another variable |
| `wrapper %command% <id>` | Runs the game under `wrapper` |
| `wrapper %command% <id> LSFG=1` | Wrapper and variables together |
| `VAR=value %command% <id>` | Unreliable. Use `<id> VAR=value` |
| `%command% <id>` | Does not launch. Never start with `%command%` |
| `<id> wrapper` | Ignored. Use the `%command%` form |
| `<id> -someflag` | Ignored. Game arguments are not supported |

---

## Troubleshooting

**A variable isn't applied.** Put it after the game id (`<id> VAR=value`), use `ALL_CAPS`
with no spaces around `=`, and check it isn't one of the launcher-managed variables above.

**LSFG does nothing.** Install the Decky LSFG-VK plugin and set up a profile, then confirm
`~/lsfg` exists.

**A wrapper does nothing.** It must go before `%command%`.

**The game won't start.** Keep the `store:game_id` token, don't start the options with
`%command%`, and don't change the shortcut's target. For Proton or prefix problems see
[Proton Compatibility → Troubleshooting](proton-compatibility.md#troubleshooting--quick-fixes).

**My options vanished.** Syncing keeps them, so re-add them and report it if they disappear
again.
