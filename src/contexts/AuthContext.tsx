/**
 * AuthContext — React wrapper around the boot-time AuthStore.
 *
 * The heavy lifting (initial fetch, EventBus subscriptions for
 * auth status changes) now lives in `stores/auth-store.ts` and
 * runs independently of QAM mount.
 *
 * This context provides:
 *   - Reactive `statuses` via `useSyncExternalStore`
 *   - User-initiated actions: `startAuth`, `logout`, `logoutAll`
 *   - `notifyConnected` — synchronous bypass for `useStoreAuth`
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
import { authStore, type StatusMap } from "../stores/auth-store";
import type { AuthResult, Result, StoreId } from "../types/api";

/** Auth context value. */
interface AuthContextValue {
  statuses: StatusMap;
  loading: boolean;
  startAuth: (store: StoreId) => Promise<AuthResult | null>;
  logout: (store: StoreId) => Promise<void>;
  logoutAll: () => Promise<void>;
  /** Called by useStoreAuth after AuthDispatcher reports
   *  success — bypasses EventBus race by setting status
   *  synchronously. */
  notifyConnected: (store: StoreId) => void;
}

const Ctx = createContext<AuthContextValue | null>(null);

/**
 * Provider that tracks per-store auth status. State comes from
 * the boot-time `authStore` singleton; user-initiated mutations
 * stay in the React layer.
 */
export const AuthProvider: FC<{ children: ReactNode }> = ({ children }) => {
  const { statuses, loading } = useSyncExternalStore(
    authStore.subscribe,
    authStore.getSnapshot,
  );

  // RPC mutations
  const startMut = useRPCMutation<[StoreId, "start"], AuthResult>(
    rpcRoutes.storeAuth,
  );
  const logoutMut = useRPCMutation<[StoreId, "logout"], Result>(
    rpcRoutes.storeAuth,
  );
  const logoutAllMut = useRPCMutation<[], Result>(rpcRoutes.clearStoreAuths);

  const startAuth = useCallback(
    (store: StoreId) => startMut.mutate(store, "start"),
    [startMut],
  );

  const logout = useCallback(
    async (store: StoreId) => {
      await logoutMut.mutate(store, "logout");
    },
    [logoutMut],
  );

  const logoutAll = useCallback(async () => {
    await logoutAllMut.mutate();
    authStore.clearAll();
    // Cleared means "unknown", which keeps every store tab; re-read the
    // real statuses so the signed-out stores' tabs go away.
    await authStore.refetch();
  }, [logoutAllMut]);

  const notifyConnected = useCallback((store: StoreId) => {
    authStore.notifyConnected(store);
  }, []);

  const value: AuthContextValue = {
    statuses,
    loading,
    startAuth,
    logout,
    logoutAll,
    notifyConnected,
  };

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
};

/**
 * Access the AuthContext value. Throws if used
 * outside `<AuthProvider>`.
 *
 * @throws Error when the provider is missing.
 */
export function useAuth(): AuthContextValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth called outside <AuthProvider>");

  return v;
}
