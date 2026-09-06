/**
 * useStoreAuth — high-level auth flow per store.
 *
 * Thin React adapter over the orchestration layer. The actual
 * multi-step handshake (backend prep → Steam shortcut launch
 * → wait for `STORE_AUTH_COMPLETE`) lives in
 * `services/auth/AuthDispatcher.ts` per the PDF spec : hooks
 * shouldn't own multi-stage coordination.
 *
 * Returned shape :
 *  - `info`        : StoreInfo for the requested store
 *  - `status`      : current StoreStatus
 *  - `connect`     : start auth, await terminal event
 *  - `disconnect`  : logout + clear local status
 *  - `busy`        : true when an auth call is in flight
 */
import { useCallback, useState } from "react";
import { call } from "@decky/api";
import { showModal } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { rpcRoutes } from "../api/rpc-routes";
import { prepareForSync } from "../lib/steam-bridge/prepare-sync";
import { useAuth } from "../contexts/AuthContext";
import { useStores } from "../contexts/StoreContext";
import { useToast } from "./useToast";
import { AuthDispatcher } from "../services/auth/AuthDispatcher";
import { ChromiumInstallModal } from "../components/modals/ChromiumInstallModal";
import { STORE_VISUALS } from "../types/store";
import { connectGameVault } from "../lib/gamevault-connect";
import type { AuthResult, StoreId } from "../types/api";

/**
 * Backend auth-failure codes that mean "the network was down /
 * flaky", not a real credential problem. Surfaced with an
 * actionable "connect to Wi-Fi" message instead of the raw code.
 * `token_exchange_network_error` — code captured but the token
 * exchange failed after retries; `network_unreachable` — the
 * browser never reached the login page (offline fast-fail).
 */
const NETWORK_ERROR_CODES = new Set([
  "token_exchange_network_error",
  "network_unreachable",
]);

/**
 * Shape returned by {@link useStoreAuth}. Bundles the
 * reactive `status` field with the action callbacks so
 * components destructure once instead of subscribing to
 * three hooks.
 */
export interface UseStoreAuthResult {
  info: ReturnType<typeof useStores>["stores"][number] | null;
  status: ReturnType<typeof useAuth>["statuses"][StoreId];
  busy: boolean;
  connect: () => Promise<AuthResult | null>;
  disconnect: () => Promise<void>;
}

/**
 * Hook that drives per-store auth flows. Delegates the
 * full handshake to {@link AuthDispatcher} and surfaces
 * the result as toasts. Status is reactive : auth events
 * trigger a re-render in `AuthContext` so the UI flips
 * Connect → Connected without polling.
 *
 * @param storeId — id of the store to drive.
 * @returns auth state + action callbacks.
 */
export function useStoreAuth(store: StoreId): UseStoreAuthResult {
  const auth = useAuth();
  const { stores } = useStores();
  const toast = useToast();
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const info = stores.find((s) => s.name === store) ?? null;
  const status = auth.statuses[store];
  // Brand name, not a translatable word — interpolated into the localized
  // sentence. Reads the same source of truth as every other store label so
  // the toast says "Epic Games", not the internal id "epic".
  const storeName = STORE_VISUALS[store]?.display_name ?? store;

  const connect = useCallback(async (): Promise<AuthResult | null> => {
    // GameVault is the only store whose sign-in is a form rather than a
    // browser OAuth or a Steam auth shortcut, and since local-vault mode it
    // is a short modal chain rather than a single dialog. AuthDispatcher
    // coordinates exactly the handshake this store does not have, so the
    // flow takes the modal route before the dispatcher is ever asked. The
    // chain itself lives in ``connectGameVault``; everything after it —
    // notifyConnected, the toasts, the post-login sync, the AuthResult shape
    // — is kept in step with the dispatcher path below by hand, so the two
    // cannot report success differently. That is not free: the sync kick was
    // missing here for exactly that reason, and a freshly connected GameVault
    // showed nothing until the user pressed Sync themselves.
    if (store === "gamevault") {
      const result = await connectGameVault({ setBusy });
      if (result === null) return null;
      if (result.success) {
        auth.notifyConnected(store);
        toast.success(t("auth.toasts.connected", { store: storeName }));
        // The dispatcher's post-login sync, which this branch never reaches.
        // Fire-and-forget for the same reason it is there: the backend queues
        // it behind any in-flight sync (SyncService._enqueue), and awaiting
        // would leave the Sign In button spinning for a whole sync.
        // ``prepareForSync`` first — a bare requestAuthSync is the gap that
        // made the automatic sync behave differently from the manual one.
        void prepareForSync()
          .then(() =>
            call<[StoreId], unknown>(rpcRoutes.requestAuthSync, store),
          )
          .catch((e) => {
            console.error(`[useStoreAuth:${store}] requestAuthSync failed:`, e);
          });
      } else {
        toast.error(
          t("auth.toasts.failed", { store: storeName }),
          result.error,
        );
      }
      return result;
    }

    setBusy(true);
    try {
      toast.info(t("auth.toasts.signingIn", { store: storeName }));
      const result = await AuthDispatcher.start(store);
      // Browser-based OAuth needs Microsoft Edge. When the
      // backend reports the prereq is missing, surface a
      // modal with an Install button rather than a useless
      // toast. The modal retries the auth flow on success.
      if (!result.success && result.error === "edge_not_installed") {
        showModal(
          <ChromiumInstallModal
            onInstalled={() => {
              // Retry the auth flow now that Edge is in.
              void connect();
            }}
            closeModal={() => {}}
          />,
        );
        return result;
      }
      if (result.success) {
        auth.notifyConnected(store);
        toast.success(t("auth.toasts.connected", { store: storeName }));
      } else if (result.error && NETWORK_ERROR_CODES.has(result.error)) {
        toast.error(
          t("auth.errors.networkTitle"),
          t("auth.errors.networkBody"),
        );
      } else {
        toast.error(
          t("auth.toasts.failed", { store: storeName }),
          result.error,
        );
      }
      return result;
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      toast.error(t("auth.toasts.failed", { store: storeName }), message);
      return { success: false, store, error: message };
    } finally {
      setBusy(false);
    }
  }, [store, storeName, toast, auth, t]);

  const disconnect = useCallback(async () => {
    setBusy(true);
    try {
      await auth.logout(store);
    } finally {
      setBusy(false);
    }
  }, [auth, store]);

  return { info, status, busy, connect, disconnect };
}
