# Feasibility Study: itch.io store

> **Implemented in 0.7.6** (`stores/itch/`, audit register 70). The current
> behaviour is in `.claude/skills/unifideck-architecture/stores.md`, measured
> against butler 15.31.0 on 2026-09-23. Several things below turned out wrong
> and are kept only as the study's record:
>
> - Sign-in is **not** a pasted key. It is the standard Edge window; the key is
>   read from the API-keys page after sign-in (behind a `/sudo` password check).
> - butler cannot be a `package.json` `remote_binary`: it is a zip, and Decky
>   never unzips. `build-plugin.sh` unpacks it into `bin/butler/`.
> - The daemon needs `--keep-alive`, or it serves one client and exits.
> - `Install.GetUploads` hides Windows uploads on SteamOS; `Fetch.GameUploads`
>   is used instead, and butler ignores the install folder passed to it.
> - itch.io is not the first store with native builds (GameVault was), and the
>   library also includes free games from the user's collections.
> - `~/feasibility-scratch/` no longer exists.

**Verdict: FEASIBLE NOW.** CLI archetype (Amazon/nile template). All four MVP steps are
documentation-proven against maintained primary sources, and the tooling was exercised on this
Steam Deck (2026-07-03): butler runs natively on SteamOS and its butlerd JSON-RPC daemon
authenticates and answers requests. The one remaining unverified step — real library enumeration
and an owned-game install — requires only a pasted API key (login-gated checklist below).

Already tracked as issue #136 ("Real API exists; large surface (butler)"). The frontend already
reserves the `"itch"` store id in `src/types/store.ts` (`StoreIdExtended`).

## MVP-bar walkthrough

| Step | Status | Evidence |
|------|--------|----------|
| Login | Proven (docs) | `Profile.LoginWithAPIKey` — documented, non-deprecated; API key is itch.io's sanctioned third-party credential (staff statement). Fully headless: user pastes a key from itch.io/user/settings/api-keys. No browser, no Edge/CDP needed. |
| Enumerate owned games | Proven (docs + live 401) | `GET api.itch.io/profile/owned-keys` (page pagination; Lutris ships it) or butlerd `Fetch.ProfileOwnedKeys` (cursor pagination + local cache). Endpoint verified alive on-device (401 JSON without key). |
| Download/install | Proven (docs) + surface verified on-device | butlerd `Install.GetUploads` → `Install.Queue` → `Install.Perform` with progress notifications; wharf delta patches for updates. Daemon handshake exercised on this Deck (see prototype log). |
| Launch | Proven with design choice | Upload `traits` (`p_linux`/`p_windows`) route native-exec vs umu/Proton. Optional `.itch.toml` manifest declares actions; manifest-less games need an exe-picking heuristic (butlerd `Launch` can do it, or route through the existing generic launcher). |

Stretch: **no platform cloud saves or achievements exist on itch.io** — nothing to build, nothing
missed relative to the Amazon/Ubisoft precedent.

## On-device prototype results (2026-07-03, scratch: `~/feasibility-scratch/itch/`)

- Fetched `https://broth.itch.zone/butler/linux-amd64/LATEST/archive/default` → 10.0 MB zip
  (butler 22.8 MB + `7z.so` + `libc7zip.so`). `butler -V` → `v15.27.0, built on May 12 2026`.
- `butler daemon --json --dbpath … --transport tcp` → emits `butlerd/listen-notification` with
  secret + TCP address; `Meta.Authenticate` → ok; `Version.Get` and `Profile.List` answered.
  Log: `~/feasibility-scratch/itch/logs/butlerd_handshake.log`.
- `api.itch.io/profile/owned-keys` unauthenticated → HTTP 401 `{"errors":["authentication required"]}`
  (endpoint alive; auth model confirmed). Note: even public game metadata requires an API key —
  there is **no anonymous path**; a free itch.io account + key covers everything including free games.

## Integration design (CLI archetype, closest to Amazon)

- **Bundle butler in `bin/`** like legendary/gogdl/nile: add to `package.json` `remote_binary`
  (sha256 + URL — broth supports pinned-version URLs: replace `LATEST` with e.g. `15.27.0`) and
  `build-plugin.sh` `prebuild_binaries()`. MIT license; 7-zip components LGPL 2.1/MPL 2.0.
- **Daemon, not one-shot CLI** — the one structural difference from nile. The backend spawns
  `butler daemon --json --dbpath ~/.local/share/unifideck/butler/butler.db --destiny-pid <plugin pid>`
  (`--destiny-pid` ties daemon lifetime to the backend) and holds a JSON-RPC connection.
  ~100 lines of asyncio; official references: itchio/cutter, itchio/node-butlerd.
- **Auth UX**: a "paste your API key" field in Store Connections — simpler than every existing
  store (no Edge, no temp shortcut, no CDP). OAuth deliberately NOT used: itch.io blocks
  OAuth-issued tokens from downloads (staff statement).
- **`get_library`**: `Fetch.ProfileOwnedKeys`; filter `classification == "game"`; overlay installed
  state from `Fetch.Caves` (satisfies the install-status overlay invariant from `stores.md`).
- **Install**: `Install.Queue`/`Install.Perform` progress events map onto the existing
  `DownloadService` progress pipeline (`services/download/`); wharf gives delta updates for free
  (Lutris still has a `# TODO` for this).
- **Launch**: `p_linux` uploads → native exec (a new native branch — itch is the first store with
  native Linux builds; xCloud's `_select_backend` precedent applies); `p_windows` → existing
  `generic_launch` under umu, per-game `WINEPREFIX` as usual. Defer HTML5/Flash uploads (filter by
  upload type).
- **unifiDB**: add `external_ids["itch"]` (numeric game id). Expect weak IGDB coverage for
  long-tail indie titles — fuzzy title match + SGDB artwork will carry more weight than for other
  stores; `cover_url` from the API is a usable artwork fallback.

## Effort estimate

**Amazon-scale, slightly above**: ~8–10 backend files / ~2k LOC (the delta over nile is the
daemon client + native-Linux launch branch), plus the standard cross-cutting touches (store
injector, frontend `StoreId`/`STORE_VISUALS`/`STORE_ICONS` — `SiItchdotio` exists in react-icons,
auth row config, rpc route). No new launcher handler for Windows uploads; a small native-exec
path for Linux uploads. **The smallest and safest of the three candidate stores.**

## Risks and caveats

- **Rate limiting (soft)**: api.itch.io 429s are common for large libraries even in the official
  app. Mitigate by going through butlerd (local DB cache) and backing off on 429.
- **Bundle gap (UX)**: games bought in mega-bundles don't appear in owned-keys until the user
  "claims" them on itch.io. Document in UI copy; not a code problem.
- **Manifest-less launch heuristics**: most itch games lack `.itch.toml`; exe-picking heuristics
  will occasionally pick wrong. butlerd `Launch` + `PickManifestAction` callbacks exist if we
  prefer to delegate.
- **ToS: friendly.** No anti-automation/third-party-client clauses; MIT client stack; staff points
  third parties at butlerd. Long-lived integrations (Lutris, Playnite, itch-dl) operate unbanned.
- **Content filtering**: itch hosts non-game and NSFW content — filter `classification`, consider a
  visibility toggle.

## OSS leverage

| Project | License | Health (2026-07-03) | Reuse |
|---------|---------|---------------------|-------|
| itchio/butler (+butlerd) | MIT | v15.27.0 2026-05-11; pushed 2026-07-03 | THE install/auth/library surface — bundle it |
| itchio/itch (official app) | MIT | v26.13.0 2026-05-18 | Reference architecture (UI shell + butlerd) |
| itchio/cutter, itchio/node-butlerd | MIT | active | butlerd handshake/protocol reference |
| lutris/lutris `services/itchio.py` (887 L) | GPL-3.0 | pushed 2026-07-03 | Pure-API alternative: owned-keys paging, traits→runner mapping. GPL — reuse approach, not code (repo is GPL-3 too, but keep provenance clean) |
| DragoonAethis/itch-dl | MIT | 2026-05-25 | Bundle-gap documentation, API usage patterns |
| Heroic | GPL-3.0 | no itch support (open request #2852) | n/a — we'd be ahead of Heroic here |

## Login-gated verification checklist (interactive follow-up)

1. Mint API key at itch.io/user/settings/api-keys (scope: full personal key).
2. butlerd `Profile.LoginWithAPIKey` → expect profile id.
3. `Fetch.ProfileOwnedKeys` → confirm full owned list (paid + claimed) and pagination behavior.
4. `Install.Queue`/`Install.Perform` an owned (or free) title into scratch; watch progress events.
5. Launch: one `p_linux` native title directly; one `p_windows` title under umu scratch prefix.

## Sources

butlerd protocol/docs: github.com/itchio/butler/blob/master/butlerd/generous/docs/README.md ·
docs.itch.zone/butlerd/master · API: itch.io/docs/api/serverside, itch.io/docs/api/oauth ·
OAuth-download block: itch.io/t/1588368 · manifest: itch.io/docs/itch/integrating/manifest.html ·
wharf: github.com/itchio/wharf-spec · broth channel: broth.itch.zone/butler/linux-amd64/versions ·
Lutris service: github.com/lutris/lutris `lutris/services/itchio.py` · bundle gap:
github.com/DragoonAethis/itch-dl · ToS: itch.io/docs/legal/terms · rate limits: itch.io/t/4345697.
On-device artifacts: `~/feasibility-scratch/itch/` (butler binary, `logs/butlerd_handshake.log`).
