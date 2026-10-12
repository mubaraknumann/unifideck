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
import { rpcRoutes } from "../api/rpc-routes";
import { prepareForSync } from "../lib/steam-bridge/prepare-sync";
import { useAuth } from "../contexts/AuthContext";
import { useStores } from "../contexts/StoreContext";
import { useToast } from "./useToast";
import {
  toastText,
  headerText,
  type ToastKey,
  type ToastString,
} from "../services/toast-text";
import { AuthDispatcher } from "../services/auth/AuthDispatcher";
import { ChromiumInstallModal } from "../components/modals/ChromiumInstallModal";
import { STORE_VISUALS } from "../types/store";
import { connectGameVault } from "../lib/gamevault-connect";
import { connectRomM } from "../lib/romm-connect";
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
 * Extra line under the "Starting sign-in" toast for stores whose sign-in has
 * a step the user would not expect. itch.io asks for the password a second
 * time before it shows the API-keys page, and a new account has to press
 * "Generate new API key" there once.
 */
const SIGN_IN_HINT_KEYS: Partial<Record<StoreId, ToastKey>> = {
  itch: "toasts.auth.hintItch",
  microsoft: "toasts.auth.hintMicrosoft",
};

/**
 * Backend sign-in failures that have a sentence of their own (the Microsoft
 * device-code flow, `MicrosoftDeviceAuth`, and itch.io's "no key on the page
 * can read the library", `stores/itch/auth.py`). Anything else is logged
 * and shows the generic reason under the "sign-in failed" title: a toast has
 * no room for a raw code.
 */
const ERROR_KEYS: Record<string, ToastKey> = {
  device_code_expired: "toasts.auth.codeExpired",
  access_denied: "toasts.auth.accessDenied",
  no_xbox_profile: "toasts.auth.noXboxProfile",
  child_account: "toasts.auth.childAccount",
  region_unavailable: "toasts.auth.regionUnavailable",
  client_rejected: "toasts.auth.clientRejected",
  itch_key_limited: "toasts.auth.itchKeyLimited",
  // With Xbox PC on, Connect Microsoft is the Xodus sign-in (I-062).
  xbox_refused: "toasts.downloadError.xboxRefused",
};

/** The reason a sign-in failed, short enough for the toast body. */
function failureReason(store: StoreId, error: string | undefined): ToastString {
  const key = error ? ERROR_KEYS[error] : undefined;
  if (key) return toastText(key);
  console.error(`[useStoreAuth:${store}] sign-in failed:`, error);
  return toastText("toasts.auth.generic");
}

/** The user pressed Cancel; telling them it failed would be noise. */
const SILENT_ERRORS = new Set(["cancelled"]);

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
  const [busy, setBusy] = useState(false);
  const info = stores.find((s) => s.name === store) ?? null;
  const status = auth.statuses[store];
  // Brand name, not a translatable word — interpolated into the localized
  // sentence. Reads the same source of truth as every other store label so
  // the toast says "Epic Games", not the internal id "epic".
  const storeName = STORE_VISUALS[store]?.display_name ?? store;

  const connect = useCallback(async (): Promise<AuthResult | null> => {
    // GameVault and RomM are the only stores whose sign-in is a form or a
    // pairing dialog rather than browser OAuth or a Steam auth shortcut, and
    // since local-vault mode GameVault is a short modal chain rather than a
    // single dialog. AuthDispatcher coordinates exactly the handshake these
    // stores do not have, so the flow takes the modal route before the
    // dispatcher is ever asked. Each chain lives in its own module
    // (``connectGameVault``, ``connectRomM``); everything after it
    // (notifyConnected, the toasts, the post-login sync, the AuthResult shape)
    // is kept in step with the dispatcher path below by hand, so the two
    // cannot report success differently. That is not free: the sync kick was
    // missing here for exactly that reason, and a freshly connected GameVault
    // showed nothing until the user pressed Sync themselves.
    if (store === "gamevault" || store === "romm") {
      const result =
        store === "gamevault"
          ? await connectGameVault({ setBusy })
          : await connectRomM({ setBusy });
      if (result === null) return null;
      if (result.success) {
        auth.notifyConnected(store);
        toast.success(
          headerText("toasts.header.store", { storeName }),
          toastText("toasts.auth.connected"),
        );
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
          headerText("toasts.header.store", { storeName }),
          failureReason(store, result.error),
        );
      }
      return result;
    }

    setBusy(true);
    try {
      const hintKey = SIGN_IN_HINT_KEYS[store];
      toast.info(
        headerText("toasts.header.store", { storeName }),
        hintKey ? toastText(hintKey) : toastText("toasts.auth.signingIn"),
      );
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
        toast.success(
          headerText("toasts.header.store", { storeName }),
          toastText("toasts.auth.connected"),
        );
      } else if (result.error && SILENT_ERRORS.has(result.error)) {
        // Cancelled by the user: nothing to report.
      } else if (result.error && NETWORK_ERROR_CODES.has(result.error)) {
        toast.error(
          headerText("toasts.header.store", { storeName }),
          toastText("toasts.auth.networkBody"),
        );
      } else {
        toast.error(
          headerText("toasts.header.store", { storeName }),
          failureReason(store, result.error),
        );
      }
      return result;
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      toast.error(
        headerText("toasts.header.store", { storeName }),
        failureReason(store, message),
      );
      return { success: false, store, error: message };
    } finally {
      setBusy(false);
    }
  }, [store, storeName, toast, auth]);

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
