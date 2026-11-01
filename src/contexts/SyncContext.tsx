/**
 * SyncContext — React wrapper around the boot-time SyncStore.
 *
 * The heavy lifting (EventBus subscriptions, progress polling,
 * Steam-restart modal) now lives in `stores/sync-store.tsx` and
 * runs independently of QAM mount.
 *
 * This context provides:
 *   - Reactive `progress`, `isSyncing`, `isCancelling` via
 *     `useSyncExternalStore`
 *   - User-initiated actions: `syncGames`, `syncImages`, `cancelSync`.
 *     The first two take a list of store ids, or nothing for every
 *     store — the Quick Access store rows sync one store at a time.
 */
import {
  createContext,
  FC,
  ReactNode,
  useCallback,
  useContext,
  useSyncExternalStore,
} from "react";
import { useRPCMutation } from "../api/useRPC";
import { rpcRoutes } from "../api/rpc-routes";
import { syncStore } from "../stores/sync-store";
import { prepareForSync } from "../lib/steam-bridge/prepare-sync";
import type { StoreId } from "../types/api";
import type { SyncProgress } from "../types/syncProgress";

/** Sync context value. */
interface SyncContextValue {
  progress: SyncProgress | null;
  isSyncing: boolean;
  isCancelling: boolean;
  /** Re-fetch these stores' libraries (all stores when omitted). */
  syncGames: (stores?: StoreId[]) => Promise<void>;
  /** Re-download these stores' artwork (all stores when omitted). */
  syncImages: (stores?: StoreId[]) => Promise<void>;
  cancelSync: () => Promise<void>;
}

const Ctx = createContext<SyncContextValue | null>(null);

/**
 * Provider that exposes sync state and actions. State comes from
 * the boot-time `syncStore` singleton; actions are thin wrappers
 * around RPC mutations that also notify the store.
 */
export const SyncProvider: FC<{ children: ReactNode }> = ({ children }) => {
  const { progress, isSyncing, isCancelling } = useSyncExternalStore(
    syncStore.subscribe,
    syncStore.getSnapshot,
  );

  const gamesMut = useRPCMutation<[StoreId[] | null], { run_id: number }>(
    rpcRoutes.syncStoreLibraries,
  );

  const imagesMut = useRPCMutation<[StoreId[] | null], { run_id: number }>(
    rpcRoutes.resyncStoreArtwork,
  );

  const cancelMut = useRPCMutation<[], { ok: boolean }>(rpcRoutes.cancelSync);

  // No `isSyncing` guard on either action: the backend queues a request
  // behind the running sync, which is what lets a second store row be
  // pressed mid-sync and show as queued.
  const syncGames = useCallback(
    async (stores?: StoreId[]) => {
      // Confirm the live active Steam user + refresh the owned-library
      // snapshot before the backend fetch, so shortcuts land in the right
      // userdata dir and Steam-linked Ubisoft games are hidden this run.
      // Shared with the post-login sync, which used to skip all of it.
      await prepareForSync();
      void gamesMut
        .mutate(stores ?? null)
        .catch((e) => console.warn("[SyncContext] syncGames RPC failed", e));
      void syncStore.refresh();
    },
    [gamesMut],
  );

  const syncImages = useCallback(
    async (stores?: StoreId[]) => {
      // No library fetch, but the run still ends in a shortcut reconcile
      // that writes shortcuts.vdf, so it needs the same active-user check.
      await prepareForSync();
      void imagesMut
        .mutate(stores ?? null)
        .catch((e) => console.warn("[SyncContext] syncImages RPC failed", e));
      void syncStore.refresh();
    },
    [imagesMut],
  );

  const cancelSync = useCallback(async () => {
    if (!isSyncing || isCancelling) return;
    syncStore.notifyCancelRequested();
    await cancelMut.mutate();
  }, [isSyncing, isCancelling, cancelMut]);

  const value: SyncContextValue = {
    progress,
    isSyncing,
    isCancelling,
    syncGames,
    syncImages,
    cancelSync,
  };

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
};

/**
 * Access the SyncContext value. Throws if used
 * outside `<SyncProvider>` — a tree-wiring bug.
 *
 * @throws Error when the provider is missing.
 */
export function useSync(): SyncContextValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSync called outside <SyncProvider>");

  return v;
}
