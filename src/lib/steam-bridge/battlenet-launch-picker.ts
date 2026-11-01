/**
 * Battle.net launch popup: pick a version, or open the client itself.
 *
 * src/lib/steam-bridge/battlenet-launch-picker.ts
 *
 * One Battle.net tile is many versions installed side by side (the World of
 * Warcraft tile is Retail, Classic and Classic Era in one prefix), and the
 * client is also where the user installs another version, repairs, or
 * changes settings. Steam already has the UI for "this game has several
 * ways to launch": the launch-options dialog it shows for real Steam apps,
 * with "Forever / Just once" per option. Steam's backend never asks for it
 * for a non-Steam shortcut, so this wraps `SteamClient.Apps.RunGame` and
 * shows that same dialog for an installed Battle.net shortcut.
 *
 * The pick is recorded through the backend (`set_battlenet_launch_choice`)
 * and only then is the shortcut started, with Steam's own `RunGame` and the
 * original arguments. The launcher consumes the pick and starts the right
 * thing. Nothing rides in the shortcut's launch options: Steam applies
 * `SetShortcutLaunchOptions` asynchronously, and a token set just before
 * `RunGame` was measured not to arrive.
 *
 * The dialog is located in Steam's bundle by its own log line, not by a
 * webpack id or export name (both change per build). When it cannot be
 * found, or the backend offers no ready version, Steam launches exactly as
 * it would without this patch.
 *
 * Whether to ask is the user's, in the shortcut's own launch options:
 * `BNET_LAUNCH_OPTIONS=yes` (the default) shows the dialog on every Play;
 * picking "Forever" there sets it to `no`, and setting it back to `yes` in
 * Steam's Properties brings the dialog back. With `no`, the version Steam
 * remembered is launched directly. When the installed versions change, that
 * memory no longer applies, so the flag returns to `yes` until the user
 * picks "Forever" again. Steam keeps the remembered pick in
 * `SteamClient.Storage` under a key derived from the option list (see
 * `rememberedKey`); "Just once" leaves no key, which is how the two differ.
 *
 * Verified against Steam's bundle on 2026-11-01: both Play call sites
 * (library Play and the game-list menu) call `SteamClient.Apps.RunGame` by
 * property lookup at call time, so wrapping the property reaches them.
 */
import { call } from "@decky/api";
import { findModuleExport } from "@decky/ui";

import { rpcRoutes } from "../../api/rpc-routes";
import { unwrapRpcEnvelope } from "../../api/useRPC";
import { getUnifideckGame } from "../library-filters";
import { toUnsignedAppId } from "./appid";

const LOG = "[BattlenetLaunchPicker]";
/** The pick that opens the Battle.net client alone, no game. */
export const CLIENT_CHOICE = "client";
/** Label of the extra option that opens the client alone. A brand name. */
export const CLIENT_OPTION_LABEL = "Battle.net";

export interface LaunchChoice {
  uid: string;
  label: string;
}

/** Steam's launch-option row, as `GetLaunchOptionsForApp` returns it. */
export interface SteamLaunchOption {
  nIndex: number;
  strDescription: string;
  strGameName: string;
  eType: number;
}

type ShowLaunchOptions = (
  props: {
    appid: number;
    continue: (index: number) => void;
    onCancel: () => void;
    alwaysShowDialog?: boolean;
  },
  options: SteamLaunchOption[],
) => Promise<void> | void;

type RunGame = (gameId: string, ...rest: unknown[]) => unknown;

/** The user's switch, in the shortcut's launch options. */
export const ASK_FLAG = "BNET_LAUNCH_OPTIONS";
const ASK_FLAG_RE = /(?<!\S)BNET_LAUNCH_OPTIONS=(\S*)/;
const DETAILS_WAIT_MS = 2000;
const FOREVER_WAIT_MS = 1500;
const POLL_MS = 100;

interface SteamStorage {
  GetString?: (key: string) => Promise<string | null | undefined>;
  DeleteKey?: (key: string) => Promise<unknown>;
}

interface AppDetailsStore {
  GetAppDetails?: (id: number) => { strLaunchOptions?: string } | undefined;
  RequestAppDetails?: (id: number) => unknown;
}

const sleep = (ms: number) =>
  new Promise<void>((resolve) => setTimeout(resolve, ms));

/** False only when the user (or "Forever") set the switch to `no`. */
export function readAskFlag(launchOptions: string | undefined): boolean {
  const match = ASK_FLAG_RE.exec(launchOptions ?? "");
  return !match || match[1].toLowerCase() !== "no";
}

/** `launchOptions` with the switch set, appended when absent. */
export function withAskFlag(launchOptions: string, ask: boolean): string {
  const token = `${ASK_FLAG}=${ask ? "yes" : "no"}`;
  return ASK_FLAG_RE.test(launchOptions)
    ? launchOptions.replace(ASK_FLAG_RE, token)
    : `${launchOptions} ${token}`.trim();
}

/** Steam's own string hash (Java-style), as its dialog keys storage with. */
export function steamHash(text: string): number {
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) {
    hash = (hash << 5) - hash + text.charCodeAt(i);
    hash |= 0;
  }
  return hash;
}

/** The `SteamClient.Storage` key Steam remembers a "Forever" pick under. */
export function rememberedKey(
  appId: number,
  options: SteamLaunchOption[],
): string {
  const hash = steamHash(JSON.stringify(options));
  const hex = (hash < 0 ? 4294967295 + hash + 1 : hash).toString(16);
  return `Apps\\${appId}\\DefaultLaunchOption\\${hex}`;
}

function storage(): SteamStorage | undefined {
  return (window.SteamClient as { Storage?: SteamStorage } | undefined)
    ?.Storage;
}

/** The option index Steam remembered for this exact list, or null. */
async function remembered(
  appId: number,
  options: SteamLaunchOption[],
): Promise<number | null> {
  try {
    const raw = await storage()?.GetString?.(rememberedKey(appId, options));
    const index = raw ? parseInt(raw, 10) : NaN;
    return Number.isInteger(index) && index >= 0 && index < options.length
      ? index
      : null;
  } catch {
    return null;
  }
}

async function forget(
  appId: number,
  options: SteamLaunchOption[],
): Promise<void> {
  try {
    await storage()?.DeleteKey?.(rememberedKey(appId, options));
  } catch {
    /* nothing remembered */
  }
}

function detailsStore(): AppDetailsStore | undefined {
  return (window as unknown as { appDetailsStore?: AppDetailsStore })
    .appDetailsStore;
}

/** The shortcut's launch options as Steam holds them, or undefined. */
async function shortcutOptions(appId: number): Promise<string | undefined> {
  const store = detailsStore();
  const read = () => store?.GetAppDetails?.(appId)?.strLaunchOptions;
  if (read() === undefined) store?.RequestAppDetails?.(appId);
  for (let waited = 0; read() === undefined && waited < DETAILS_WAIT_MS; ) {
    await sleep(POLL_MS);
    waited += POLL_MS;
  }
  return read();
}

async function setAskFlag(appId: number, ask: boolean): Promise<void> {
  const current = await shortcutOptions(appId);
  const apps = window.SteamClient?.Apps as
    | { SetShortcutLaunchOptions?: (id: number, opts: string) => void }
    | undefined;
  if (current === undefined || !apps?.SetShortcutLaunchOptions) return;
  const next = withAskFlag(current, ask);
  if (next !== current) apps.SetShortcutLaunchOptions(appId, next);
}

/** "Forever" leaves a remembered pick behind; "Just once" does not. */
async function noteForever(
  appId: number,
  options: SteamLaunchOption[],
): Promise<void> {
  for (let waited = 0; waited < FOREVER_WAIT_MS; waited += POLL_MS) {
    if ((await remembered(appId, options)) !== null) {
      console.log(`${LOG} "Forever" picked for ${appId}; not asking again`);
      await setAskFlag(appId, false);
      return;
    }
    await sleep(POLL_MS);
  }
}

let dialog: ShowLaunchOptions | null | undefined;

function findDialog(): ShowLaunchOptions | null {
  if (dialog === undefined) {
    try {
      dialog =
        (findModuleExport(
          (e: unknown) =>
            typeof e === "function" &&
            String(e).includes("showing launch options"),
        ) as ShowLaunchOptions | undefined) ?? null;
    } catch (error) {
      console.error(`${LOG} dialog lookup failed:`, error);
      dialog = null;
    }
    if (!dialog) {
      console.warn(
        `${LOG} Steam's launch-options dialog was not found in this build — ` +
          "Battle.net games launch without the version popup",
      );
    }
  }
  return dialog;
}

/** The Steam appid inside a shortcut's 64-bit game id, or null. */
export function appIdFromGameId(gameId: string): number | null {
  try {
    const appId = Number(BigInt(gameId) >> 32n);
    return appId > 0 ? appId : null;
  } catch {
    return null;
  }
}

/** An installed Battle.net shortcut, as the picker addresses it. */
export interface BattlenetGame {
  appId: number;
  /** The store's own id (`wow`): what the store's id map is keyed on. */
  uid: string;
}

/** The installed Battle.net shortcut behind a game id, else null. */
export function battlenetGameFor(gameId: string): BattlenetGame | null {
  const appId = appIdFromGameId(gameId);
  if (appId === null) return null;
  const unsigned = toUnsignedAppId(appId);
  for (const candidate of [unsigned, unsigned | 0]) {
    const game = getUnifideckGame(candidate);
    if (game?.store === "battlenet" && game.isInstalled && game.storeGameId) {
      return { appId: unsigned, uid: game.storeGameId };
    }
  }
  return null;
}

/** The dialog's rows: every installed version, then the client itself. */
export function buildOptions(choices: LaunchChoice[]): SteamLaunchOption[] {
  const rows = choices.map((choice, index) => ({
    nIndex: index,
    strDescription: choice.label,
    strGameName: "",
    eType: 0,
  }));
  rows.push({
    nIndex: choices.length,
    strDescription: CLIENT_OPTION_LABEL,
    strGameName: "",
    eType: 0,
  });
  return rows;
}

/** What the launcher is told for a picked row: a version uid, or the client. */
export function choiceFor(choices: LaunchChoice[], index: number): string {
  return choices[index]?.uid ?? CLIENT_CHOICE;
}

async function fetchChoices(uid: string): Promise<LaunchChoice[]> {
  const raw = await call<[string], unknown>(
    rpcRoutes.getBattlenetLaunchChoices,
    uid,
  ).catch((error: unknown) => {
    console.error(`${LOG} could not list versions for ${uid}:`, error);
    return null;
  });
  if (raw == null) return [];
  const payload = unwrapRpcEnvelope<{ choices?: LaunchChoice[] }>(raw, {
    route: rpcRoutes.getBattlenetLaunchChoices,
    throwing: false,
  });
  return Array.isArray(payload?.choices) ? payload.choices : [];
}

/** Record the pick; true once the backend has stored it for the launcher. */
async function recordChoice(uid: string, choice: string): Promise<boolean> {
  const raw = await call<[string, string], unknown>(
    rpcRoutes.setBattlenetLaunchChoice,
    uid,
    choice,
  ).catch((error: unknown) => {
    console.error(`${LOG} could not record ${choice} for ${uid}:`, error);
    return null;
  });
  if (raw == null) return false;
  const payload = unwrapRpcEnvelope<{ recorded?: boolean }>(raw, {
    route: rpcRoutes.setBattlenetLaunchChoice,
    throwing: false,
  });
  return payload?.recorded === true;
}

async function recordAndLaunch(
  uid: string,
  choice: string,
  launch: () => void,
): Promise<void> {
  if (!(await recordChoice(uid, choice))) {
    console.error(
      `${LOG} ${choice} was not recorded for ${uid}; not launching`,
    );
    return;
  }
  launch();
}

async function pick(
  game: BattlenetGame,
  show: ShowLaunchOptions,
  launch: () => void,
): Promise<void> {
  const choices = await fetchChoices(game.uid);
  if (choices.length === 0) {
    console.warn(
      `${LOG} no ready version found for ${game.uid} — launching as usual`,
    );
    launch();
    return;
  }
  const options = buildOptions(choices);
  if (!readAskFlag(await shortcutOptions(game.appId))) {
    const index = await remembered(game.appId, options);
    if (index !== null) {
      await recordAndLaunch(game.uid, choiceFor(choices, index), launch);
      return;
    }
    // Versions were installed or removed: the remembered pick belonged to
    // another list. Back to the default until "Forever" is picked again.
    console.log(`${LOG} installed versions changed for ${game.uid}; asking`);
  }
  await setAskFlag(game.appId, true);
  // A stale pick would read as "Forever" the moment the dialog closes.
  await forget(game.appId, options);
  await show(
    {
      appid: game.appId,
      alwaysShowDialog: true,
      continue: (index) => {
        void recordAndLaunch(game.uid, choiceFor(choices, index), launch);
        void noteForever(game.appId, options);
      },
      onCancel: () => console.log(`${LOG} launch of ${game.uid} cancelled`),
    },
    options,
  );
}

/**
 * Wrap `SteamClient.Apps.RunGame` so Play on a Battle.net shortcut shows
 * the version popup. Returns null when Steam's launch API is missing.
 */
export function applyBattlenetLaunchPicker(): { unpatch: () => void } | null {
  const apps = window.SteamClient?.Apps as { RunGame?: RunGame } | undefined;
  const original = apps?.RunGame;
  if (!apps || typeof original !== "function") return null;

  const wrapped: RunGame = function (this: unknown, gameId, ...rest) {
    const launch = (): unknown => original.call(this, gameId, ...rest);
    const game = battlenetGameFor(String(gameId));
    const show = game ? findDialog() : null;
    if (!game || !show) return launch();
    void pick(game, show, launch).catch((error: unknown) => {
      console.error(`${LOG} popup failed, launching as usual:`, error);
      launch();
    });
    return undefined;
  };
  apps.RunGame = wrapped;
  return {
    unpatch: () => {
      if (apps.RunGame === wrapped) apps.RunGame = original;
    },
  };
}
