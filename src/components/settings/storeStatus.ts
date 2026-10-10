/**
 * storeStatus — the one line of status text each store row shows.
 *
 * Pure, so the rules live in one tested place and the rows stay layout
 * only. A store row reads, in order of precedence:
 *
 *   1. `Not connected`        — signed out; nothing else applies.
 *   2. its row in the current run (`SyncProgress.stores[id]`):
 *        queued  → `Queued`
 *        active  → `Games 41/120` while fetching, `Images 62%` after
 *        waiting → `Waiting…` (its part is done, the run is not)
 *        error   → `Sync failed` (stays until the next run)
 *        done / cancelled → `Done` / `Cancelled` briefly, then idle
 *   3. a queued request that names it → `Queued`
 *   4. idle → `340 games · 2 hr. ago`, or `Not synced yet`
 *
 * `detail` is the longer form for the expanded row: full counts while a
 * phase runs, or the error message after a failure.
 */
import type { TFunction } from "i18next";
import type {
  QueuedSync,
  StoreSyncPhase,
  StoreSyncRow,
  StoreSyncSummary,
} from "../../types/syncProgress";

export interface StoreStatusLine {
  text: string;
  tone: "normal" | "error";
  detail?: string;
}

export interface StoreStatusInput {
  connected: boolean;
  row?: StoreSyncRow;
  summary?: StoreSyncSummary;
  queued: boolean;
  /** The row turned `done` / `cancelled` moments ago, so say so. */
  recentlyFinished: boolean;
  /** Current time, unix ms. */
  now: number;
  language: string;
}

/** i18n key for each phase's label. Explicit, so every key is greppable. */
const PHASE_KEYS: Record<Exclude<StoreSyncPhase, "">, string> = {
  games: "storeConnections.phaseGames",
  metadata: "storeConnections.phaseMetadata",
  artwork: "storeConnections.phaseArtwork",
  compat: "storeConnections.phaseCompat",
};

/** Whether `queued` names `store` (`stores: null` names every store). */
export function isQueued(
  queued: QueuedSync | null | undefined,
  store: string,
): boolean {
  if (!queued) return false;
  return queued.stores === null || queued.stores.includes(store);
}

/** A unix-seconds timestamp as "2 hr. ago" in `language`. */
export function formatAgo(
  syncedAtSecs: number,
  now: number,
  language: string,
): string {
  const secs = Math.round(syncedAtSecs - now / 1000);
  const abs = Math.abs(secs);
  const [value, unit]: [number, Intl.RelativeTimeFormatUnit] =
    abs < 60
      ? [0, "second"]
      : abs < 3600
      ? [Math.round(secs / 60), "minute"]
      : abs < 86400
      ? [Math.round(secs / 3600), "hour"]
      : [Math.round(secs / 86400), "day"];
  try {
    return new Intl.RelativeTimeFormat(language, {
      numeric: "auto",
      style: "short",
    }).format(value, unit);
  } catch {
    // An unknown language tag throws; fall back to the runtime default.
    return new Intl.RelativeTimeFormat(undefined, {
      numeric: "auto",
      style: "short",
    }).format(value, unit);
  }
}

function phaseLabel(t: TFunction, phase: StoreSyncPhase): string {
  return phase ? t(PHASE_KEYS[phase]) : "";
}

function activeLine(t: TFunction, row: StoreSyncRow): StoreStatusLine {
  const label = phaseLabel(t, row.phase);
  const detail = `${label}: ${row.done} / ${row.total}`;
  // Library fetch counts are small and meaningful; the post-sync phases
  // walk the whole library, where a percentage reads better.
  if (row.phase === "games") {
    return {
      text: `${label} ${row.done}/${row.total}`,
      tone: "normal",
      detail,
    };
  }
  const percent = row.total > 0 ? Math.floor((row.done / row.total) * 100) : 0;
  return { text: `${label} ${percent}%`, tone: "normal", detail };
}

function idleLine(t: TFunction, input: StoreStatusInput): StoreStatusLine {
  const { summary } = input;
  if (!summary || summary.synced_at === null) {
    return { text: t("storeConnections.notSynced"), tone: "normal" };
  }
  const games = t("storeConnections.gameCount", { count: summary.count });
  const ago = formatAgo(summary.synced_at, input.now, input.language);
  return { text: `${games} · ${ago}`, tone: "normal" };
}

function runLine(
  t: TFunction,
  row: StoreSyncRow,
  input: StoreStatusInput,
): StoreStatusLine | null {
  switch (row.state) {
    case "queued":
      return { text: t("storeConnections.queued"), tone: "normal" };
    case "active":
      return activeLine(t, row);
    case "waiting":
      return { text: t("storeConnections.waiting"), tone: "normal" };
    case "error":
      return {
        text: t("storeConnections.failed"),
        tone: "error",
        detail: row.error ?? undefined,
      };
    case "done":
      return input.recentlyFinished
        ? { text: t("storeConnections.done"), tone: "normal" }
        : null;
    case "cancelled":
      return input.recentlyFinished
        ? { text: t("storeConnections.cancelled"), tone: "normal" }
        : null;
    default:
      return null;
  }
}

/** The status line for one store row. */
export function formatStoreStatus(
  t: TFunction,
  input: StoreStatusInput,
): StoreStatusLine {
  if (!input.connected) {
    return { text: t("storeConnections.notConnected"), tone: "normal" };
  }
  const fromRun = input.row ? runLine(t, input.row, input) : null;
  if (fromRun) return fromRun;
  if (input.queued) {
    return { text: t("storeConnections.queued"), tone: "normal" };
  }
  return idleLine(t, input);
}

/**
 * The status line for the "All stores" row: how many of the run's stores
 * have finished, `Queued`, or the whole library's game count when idle.
 */
export function formatAllStoresStatus(
  t: TFunction,
  rows: Record<string, StoreSyncRow> | undefined,
  summary: Record<string, StoreSyncSummary> | undefined,
  queued: QueuedSync | null | undefined,
  isSyncing: boolean,
): StoreStatusLine {
  const list = Object.values(rows ?? {});
  if (isSyncing && list.length > 0) {
    const finished = list.filter((r) =>
      ["done", "error", "cancelled"].includes(r.state),
    ).length;
    return {
      text: t("storeConnections.storesDone", {
        done: finished,
        total: list.length,
      }),
      tone: "normal",
    };
  }
  if (queued?.stores === null) {
    return { text: t("storeConnections.queued"), tone: "normal" };
  }
  const total = Object.values(summary ?? {}).reduce((n, s) => n + s.count, 0);
  return {
    text: t("storeConnections.gameCount", { count: total }),
    tone: "normal",
  };
}
