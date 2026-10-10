/**
 * storeStatus — the one line of status text each store row shows.
 *
 * Pure, so the rules live in one tested place and the rows stay layout
 * only. A store row reads, in order of precedence:
 *
 *   1. blank                  — signed out; the grey icon says so.
 *   2. its row in the current run (`SyncProgress.stores[id]`):
 *        queued  → `Queued`
 *        active  → `41/120 games`, or `210/340 images` while artwork
 *                   downloads — number first, like the idle `340 games`
 *        waiting → `Waiting…` (its part is done, the run is not)
 *        error   → `Sync failed` (stays until the next run)
 *        done / cancelled → `Done` / `Cancelled` briefly, then idle
 *   3. a queued request that names it → `Queued`
 *   4. idle → `340 games`, or `Not synced yet`
 *
 * Progress lives only in the row. `detail` is shown at the top of an
 * expanded row, above its buttons, and carries just what the row cannot: a failure's message.
 */
import type { TFunction } from "i18next";
import type {
  QueuedSync,
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
}

/** Whether `queued` names `store` (`stores: null` names every store). */
export function isQueued(
  queued: QueuedSync | null | undefined,
  store: string,
): boolean {
  if (!queued) return false;
  return queued.stores === null || queued.stores.includes(store);
}

function activeLine(t: TFunction, row: StoreSyncRow): StoreStatusLine {
  // Every phase walks this store's games one at a time, so it reads as a
  // count of them, number first like the idle "340 games". Artwork is the
  // one step the user knows as images. `count` drives the plural.
  const key =
    row.phase === "artwork"
      ? "storeConnections.progressImages"
      : "storeConnections.progressGames";
  return {
    text: t(key, { done: row.done, count: row.total }),
    tone: "normal",
  };
}

function idleLine(t: TFunction, input: StoreStatusInput): StoreStatusLine {
  const { summary } = input;
  if (!summary) {
    return { text: t("storeConnections.notSynced"), tone: "normal" };
  }
  return {
    text: t("storeConnections.gameCount", { count: summary.count }),
    tone: "normal",
  };
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
    return { text: "", tone: "normal" };
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
