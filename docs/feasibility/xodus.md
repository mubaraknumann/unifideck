# Evaluation: Xodus (Xbox PC / Game Pass on Linux)

**Verdict: watch, don't build yet. Integrate as a client later; never fork.**
Evaluated 2026-09-24 from the GitHub repos, issues and news coverage. Nothing was built or run.

Xodus (github.com/xodus-gaming/xodus) is an unofficial, reverse-engineered project that aims to
download, license and run Xbox PC games, Game Pass included, on Linux. It would let our existing
Microsoft store offer *installable* Xbox PC games next to Xbox Cloud Gaming.

## Maturity: pre-alpha

- **No releases.** The CLI describes itself as "used for iterating over new xodus features". The
  README says the parts "are still quite scattered around".
- **Works:** device and user sign-in, Xbox authorization, MSIXVC package download, license
  acquisition, on-demand exe decryption.
- **Not yet:**
  - MSIXVC2 packages (#53).
  - Launching was broken on Linux until recently (#150, closed).
  - Games that call Xbox services need more Wine work ("soon™" for Minecraft Bedrock).
  - Gears of War 4 is named as unsupported.
  - `xodus-service`, the planned integration point, is still being designed (PRs #190, #193).
- **Active, capable team.** 1.5k stars, 49 forks, about 40 commits from July to September 2026
  (latest 2026-09-23), 12 contributors. Most work is by imLinguin (Heroic; author of gogdl and
  nile, which we bundle), Vidi0 and ChristopherHX. BellezaEmporium (EA Galaxy plugin) contributes too.

## The stack

Xodus is four projects that must move together:

| Repo | Language | License | Role |
|---|---|---|---|
| `xodus` | Rust | GPL-3.0 | core, CLI, `xodus-service` (IPC over `xodus.sock`) |
| `xgameruntime` | C | LGPL-2.1 | open reimplementation of `xgameruntime.dll` |
| `wine` | C | Wine (LGPL) | fork that ships `xgameruntime` and maps decrypted images from memory |
| `Proton` | C++ | mixed | Proton fork |

Also: `xal-rs` (MIT, Xbox sign-in), `ntfs` (Apache-2.0), `eappx-rs` (MIT).

Games stay encrypted on disk and are decrypted into memory at run time, so they run only on the
Xodus Wine build. Stock Proton or GE-Proton cannot run them.

## Why we would not take over or fork it

- **Maintenance.** Owning it means owning a Rust codebase plus a Wine fork plus a Proton fork. We
  are a Python/TypeScript project and do not maintain a Proton.
- **It splits an active effort.** We would compete with the people best placed to do this
  reverse engineering, and absorb every Microsoft change without them.
- **Legal exposure.** Xodus extracts content keys (its `license` command "dumps CIKs") and
  decrypts protected packages, which is anti-circumvention territory. That risk now sits with an
  unaffiliated project that says "use at your own risk". Forking it, or bundling it by default,
  moves part of that risk onto Unifideck. It could also affect a Decky store listing (#25).
- **License is not the blocker.** Both projects are GPL-3.0, so reuse would be allowed.

## How we would integrate

The Xodus architecture doc lists this requirement: "Users want to use their launcher of
preference, not another launcher. Integration should be simple." That is our use case.

1. **Wait for the gate:** a tagged release, a stable `xodus-service` IPC, and a runnable Wine
   build we can download instead of compile.
2. **Extend the existing Microsoft store**, not a new store. We already sign in to Microsoft for
   xCloud; compare that flow with `xal-rs` before adding a second sign-in.
3. **Treat Xodus as an external tool**, like legendary, gogdl and nile: a pinned binary, and games
   run on the Xodus Wine. Opt-in and labelled experimental.
4. **Contribute upstream** what the Deck needs. Their blank sign-in window on KDE Wayland (#167)
   is a problem we already solved for our own sign-in windows.

## Recheck when

- Xodus tags its first release, or
- `xodus-service` has a documented, stable IPC (watch #190), or
- MSIXVC2 support lands (#53).

## References

- github.com/xodus-gaming/xodus: README, `docs/xodus/architecture.md`, issues #50, #53, #150, #167, #190, #193
- github.com/xodus-gaming (org): `xgameruntime`, `wine`, `Proton`, `xal-rs`
- GamingOnLinux, 2026-08-11: "XBOX PC and Game Pass coming to Linux with the 'Xodus' project"
