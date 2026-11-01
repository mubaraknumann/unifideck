/**
 * resolvePlaySection: the pure decision behind `usePlaySection`.
 *
 * Given the game info, the download queue and the Xbox PC status, pick the
 * Play-section state. No hooks and no I/O, so every branch is unit-tested
 * (`usePlaySection.test.ts`).
 *
 * Rows tagged `xbox_pc` get their own table, below. Every other row keeps
 * the order it always had: browser games first (xCloud streams and itch.io
 * HTML5 games), then the live queue, then `is_installed`.
 *
 * Xbox PC rows (keyed by tag, never by store name):
 *
 * | Situation                         | State                                  |
 * |-----------------------------------|----------------------------------------|
 * | live queue row                    | downloading                            |
 * | status still loading              | skeleton (`steam-native`, no override) |
 * | status error, installed           | installed (the launcher reads config)  |
 * | status error, not installed       | Stream if `xcloud`, else unsupported   |
 * | enabled, installed                | installed                              |
 * | enabled, not supported / choice   | Stream + reason if `xcloud`, else unsupported |
 * | enabled, otherwise                | Install (+ secondary Stream if `xcloud`) |
 * | disabled, installed               | unsupported `disabled` with Uninstall  |
 * | disabled, not installed           | Stream if `xcloud`, else unsupported   |
 *
 * There is no Stream on an installed row in v1: Play on an installed row
 * runs the PC game.
 */
import type { DownloadItem, DownloadQueueInfo } from "../types/downloads";
import type { Game, XboxPcInfo } from "../types/api";
import type { XboxPcSnapshot } from "../stores/xbox-pc-store";
import {
  asPcUnsupportedReason,
  type PcUnsupportedReason,
} from "../lib/xbox-pc-errors";

/**
 * Discriminated union of the Play-section states. Drives
 * which variant of the Play button is rendered
 * (NotInstalled / Downloading / Installed / Launching).
 */
export type PlaySectionState =
  | { kind: "steam-native"; shouldOverride: false }
  | { kind: "unavailable"; shouldOverride: false }
  | {
      kind: "not-installed";
      shouldOverride: true;
      installable: true;
      /** Set on a merged Xbox PC + xCloud row: a secondary Stream button. */
      streamUrl?: string;
    }
  | { kind: "downloading"; shouldOverride: true; download: DownloadItem }
  | { kind: "installed"; shouldOverride: true; appId: number }
  | {
      kind: "browser";
      shouldOverride: true;
      appId: number;
      /** "stream" = xCloud (Play on Cloud); "web" = an HTML5 game. */
      variant: "stream" | "web";
      url: string;
      /** Why the PC version cannot install, shown under Stream. */
      notice?: PcUnsupportedReason;
    }
  | {
      kind: "pc-unsupported";
      shouldOverride: true;
      appId: number;
      reason: PcUnsupportedReason;
      /** Installed rows offer Uninstall. */
      installed: boolean;
    };

/** What `useGameInfo` reports, reduced to what the resolver reads. */
export interface GameInfoView {
  data: Game | null;
  status?: string;
}

/** The Xbox PC store snapshot, as the resolver reads it. */
export type XboxPcView = Pick<XboxPcSnapshot, "status" | "loading" | "error">;

/** Pick the Play-section state; see the module docstring. */
export function resolvePlaySection(
  appId: number | null,
  info: GameInfoView,
  queue: DownloadQueueInfo | null | undefined,
  xbox: XboxPcView,
): PlaySectionState {
  if (appId != null && !info.data && info.status === "unavailable") {
    // No answer from the backend. Waiting longer only keeps a blank
    // page up; Steam's own section at least lets the user play.
    return { kind: "unavailable", shouldOverride: false };
  }
  if (appId == null || !info.data) {
    return { kind: "steam-native", shouldOverride: false };
  }

  const game = info.data;
  // Filter out games whose store is plain Steam — we never override those.
  if (game.store === "steam") {
    return { kind: "steam-native", shouldOverride: false };
  }

  const tags = game.store_tags ?? [];
  if (tags.includes("xbox_pc")) {
    return resolveXboxPc(appId, game, queue, xbox);
  }

  // Browser games (xCloud streams, itch.io HTML5 games) open in an Edge
  // window and install nothing, so they are always playable: surface a
  // Play state regardless of is_installed. The launcher finds the URL in
  // the library cache (backend `launcher/browser_games`). `xcloud` alone
  // still counts, for a library cached before the `browser` tag existed.
  if (tags.includes("browser") || tags.includes("xcloud")) {
    return browserState(appId, game);
  }

  // Check the live queue first — a download in progress
  // takes precedence over `is_installed` flag staleness.
  const inQueue = queueRow(queue, game.id);
  if (inQueue) {
    return { kind: "downloading", shouldOverride: true, download: inQueue };
  }
  if (!game.is_installed) {
    return { kind: "not-installed", shouldOverride: true, installable: true };
  }

  return { kind: "installed", shouldOverride: true, appId };
}

/** The table in the module docstring. */
function resolveXboxPc(
  appId: number,
  game: Game,
  queue: DownloadQueueInfo | null | undefined,
  xbox: XboxPcView,
): PlaySectionState {
  // A live download always shows, so Cancel stays reachable whatever the
  // toggle says.
  const inQueue = queueRow(queue, game.id);
  if (inQueue) {
    return { kind: "downloading", shouldOverride: true, download: inQueue };
  }
  const installed = Boolean(game.is_installed);
  const stream = (game.store_tags ?? []).includes("xcloud");
  const status = xbox.status;
  if (!status) {
    if (!xbox.error) return { kind: "steam-native", shouldOverride: false };
    if (installed) return { kind: "installed", shouldOverride: true, appId };
    return stream
      ? browserState(appId, game)
      : unsupported(appId, "status_unavailable", false);
  }
  if (!status.enabled) {
    if (installed) return unsupported(appId, "disabled", true);
    return stream
      ? browserState(appId, game)
      : unsupported(appId, "disabled", false);
  }
  if (installed) return { kind: "installed", shouldOverride: true, appId };
  const blocked = blockedReason(game.xbox_pc);
  if (blocked) {
    return stream
      ? { ...browserState(appId, game), notice: blocked }
      : unsupported(appId, blocked, false);
  }
  return {
    kind: "not-installed",
    shouldOverride: true,
    installable: true,
    ...(stream ? { streamUrl: streamUrlOf(game) } : {}),
  };
}

/**
 * Why the row's own facts block an install, or null when Install may try.
 *
 * `choice` needs a package picker v1 does not have, unless the backend
 * already picked the one Game (`content_id` set). `not_supported` names
 * its plan in `reason` (or `access`); anything unrecognised reads as
 * `unavailable` rather than offering an Install that the backend refuses.
 */
function blockedReason(
  info: XboxPcInfo | undefined,
): PcUnsupportedReason | null {
  if (!info) return null;
  if (info.state === "choice") {
    // One supported Game beside add-ons is picked for the user: the row
    // carries its content id and no `needs_choice` reason.
    const picked = Boolean(info.content_id) && info.reason !== "needs_choice";
    return picked ? null : "needs_choice";
  }
  if (info.state !== "not_supported") return null;
  return (
    asPcUnsupportedReason(info.reason) ??
    asPcUnsupportedReason(info.access) ??
    "unavailable"
  );
}

function unsupported(
  appId: number,
  reason: PcUnsupportedReason,
  installed: boolean,
): PlaySectionState {
  return {
    kind: "pc-unsupported",
    shouldOverride: true,
    appId,
    reason,
    installed,
  };
}

/** The xCloud play URL for a row, or its browser URL. */
function streamUrlOf(game: Game): string {
  return game.browser_url ?? `https://www.xbox.com/play/launch/${game.id}`;
}

function browserState(
  appId: number,
  game: Game,
): Extract<PlaySectionState, { kind: "browser" }> {
  const stream = (game.store_tags ?? []).includes("xcloud");
  return {
    kind: "browser",
    shouldOverride: true,
    appId,
    variant: stream ? "stream" : "web",
    url: stream ? streamUrlOf(game) : game.browser_url ?? "",
  };
}

/**
 * Statuses meaning the item is finished with. A finished download must never
 * render as an active one: the "downloading" branch above deliberately
 * outranks `is_installed`, so a stale row does not just look wrong, it hides
 * the Play button behind a progress bar whose Cancel cannot succeed.
 *
 * The backend filters these out of `current` too (`get_queue`). This is the
 * second line of defence, and it is worth having: the queue snapshot is only
 * replaced when a download event arrives, there is no polling refetch, and the
 * event that clears a row is the same one that races the backend's own
 * cleanup.
 */
const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "complete",
  "failed",
  "cancelled",
]);

function isTerminal(item: DownloadItem): boolean {
  return TERMINAL_STATUSES.has(item.status);
}

/** The live queue row for `gameId`, current or pending. */
function queueRow(
  queue: DownloadQueueInfo | null | undefined,
  gameId: string,
): DownloadItem | null {
  const current = queue?.current;
  if (current && !isTerminal(current) && current.game_id === gameId) {
    return current;
  }
  return (
    queue?.queued?.find((d) => d.game_id === gameId && !isTerminal(d)) ?? null
  );
}
