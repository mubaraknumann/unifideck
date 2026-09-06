/**
 * store-status — the one "is this store signed in?" probe.
 *
 * Extracted from `AuthDispatcher` so the storefront flow asks the same
 * question the same way. Two callers now depend on it, and both use the
 * answer to decide whether to run a library sync — a decision that must
 * never be made from two different sources of truth, because a sync
 * against a logged-out store REMOVES that store's shortcuts (the
 * post-sync reconcile sweep).
 */
import { call } from "@decky/api";
import { rpcRoutes } from "../../api/rpc-routes";
import { unwrapRpcEnvelope } from "../../api/useRPC";
import type { StoreId } from "../../types/api";

/**
 * Whether the backend currently considers ``store`` signed in.
 *
 * Reuses ``check_store_status`` — the probe behind the stores tab's
 * badges — rather than adding a second source of truth for the same
 * question. Any failure answers ``false``: this only ever *gates* work
 * that a signed-out store must not have done, so an unreachable backend
 * must skip, never proceed.
 */
export async function storeReportsConnected(store: StoreId): Promise<boolean> {
  try {
    const raw = await call<[], unknown>(rpcRoutes.checkStoreStatus);
    const data = unwrapRpcEnvelope<unknown>(raw, {
      route: rpcRoutes.checkStoreStatus,
      throwing: false,
    });
    if (!Array.isArray(data)) return false;
    return data.some(
      (e) =>
        e &&
        typeof e === "object" &&
        (e as Record<string, unknown>).store_id === store &&
        Boolean((e as Record<string, unknown>).available),
    );
  } catch {
    return false;
  }
}
