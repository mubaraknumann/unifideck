# Proton Compatibility Settings

Unifideck supports custom Proton versions for Windows games. By default, **Proton Experimental** is used.

## Quick Start

Add `PROTON=` to your game's launch options:

```
PROTON=GE-Proton10-10 epic:gameid
```

Or after the game ID:
```
epic:gameid PROTON=GE-Proton10-10
```

## Methods

### Method 1: Launch Options (Recommended)
Add to your game's Steam shortcut launch options:
```
PROTON=GE-Proton10-10 epic:7334aba246154b63857435cb9c7eecd5
```

### Method 2: Custom Proton Path
For custom Proton installations:
```
PROTONPATH=/path/to/custom/proton epic:gameid
```

## Combining with Other Options

You can combine Proton selection with other launch options:
```
LSFG=1 PROTON=GE-Proton9-26 MANGOHUD=1 gog:12345
```

## Priority Order

1. `PROTONPATH=` (highest priority)
2. `PROTON=` name
3. Proton Experimental (default)

## Troubleshooting

If a game crashes with default Proton, try a GE-Proton version:
```
PROTON=GE-Proton10-10 epic:gameid
```

If you see a message that says 'Path Not Found', try running the game again. If that doesn't work, delete the prefix and try again.

GE-Proton includes additional patches and fixes not in official Proton.
