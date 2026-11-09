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
 *   - User-initiated actions: `startSync`, `forceSync`, `cancelSync`
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
import { linkXboxPc } from "../services/store/xbox-pc-link";
import type { SyncProgress } from "../types/syncProgress";

/** Sync context value. */
interface SyncContextValue {
  progress: SyncProgress | null;
  isSyncing: boolean;
  isCancelling: boolean;
  startSync: () => Promise<void>;
  forceSync: (resyncArtwork?: boolean) => Promise<void>;
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

  const startMut = useRPCMutation<[], { run_id: number }>(
    rpcRoutes.syncLibraries,
  );

  const forceMut = useRPCMutation<[boolean?], { run_id: number }>(
    rpcRoutes.forceSyncLibraries,
  );

  const cancelMut = useRPCMutation<[], { ok: boolean }>(rpcRoutes.cancelSync);

  const startSync = useCallback(async () => {
    if (isSyncing) return;
    // A signed-out Xbox PC has no other way back to the sign-in. It resyncs by
    // itself when it ends, and that sync queues behind this one.
    void linkXboxPc({ auto: true });
    // Confirm the live active Steam user + refresh the owned-library snapshot
    // before the backend fetch, so shortcuts land in the right userdata dir
    // and Steam-linked Ubisoft games are hidden this run. Shared with the
    // post-login sync, which used to skip all of it.
    await prepareForSync();
    void startMut
      .mutate()
      .catch((e) => console.warn("[SyncContext] startSync RPC failed", e));
  }, [isSyncing, startMut]);

  const forceSync = useCallback(
    async (resyncArtwork?: boolean) => {
      void linkXboxPc({ auto: true });
      await prepareForSync();
      void forceMut
        .mutate(resyncArtwork)
        .catch((e) => console.warn("[SyncContext] forceSync RPC failed", e));
    },
    [forceMut],
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
    startSync,
    forceSync,
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
