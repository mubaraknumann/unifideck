/**
 * Read a per-store capability off the store-info payload.
 *
 * Replaces the hand-written `new Set(["gog", "epic"])` pattern. The 2026-08
 * architecture audit found **sixteen** such lists in `src/`, of which exactly
 * one pair was machine-checked. Two were duplicated inside TypeScript alone:
 * `CLOUD_SAVE_STORES` existed in both `useCloudSaveStatus.ts` and
 * `PlayMeta.tsx`, the second carrying a comment admitting it mirrored the
 * first.
 *
 * The failure mode is silent in both directions. A store added to the backend
 * only shows no button; a store added to the frontend only shows a button that
 * raises. Neither surfaces as an error, which is why they drifted unnoticed.
 *
 * Capabilities are now derived server-side in `core/store_capabilities.py`
 * from the same sets the RPC mixins and services branch on, injected into
 * `get_store_infos`, and pinned by `tests/unit/test_store_capabilities.py`
 * against the code that implements them — the registered cloud-save
 * strategies, the stores defining `get_game_achievements`, the exposed
 * language RPCs. So this reads one value rather than restating a policy.
 *
 * Fails **closed**: while the payload is still loading, or for a store the
 * backend did not report, every capability reads `false`. A missing button is
 * recoverable; a button that calls an unsupported route is not.
 */
import { useSyncExternalStore } from "react";
import { storeInfoStore } from "../stores/store-info-store";
import type { StoreCapability } from "../types/api";

/** True if `store` reports `capability`. False while loading or unknown. */
export function useStoreCapability(
  store: string | undefined | null,
  capability: StoreCapability,
): boolean {
  const snapshot = useSyncExternalStore(
    storeInfoStore.subscribe,
    storeInfoStore.getSnapshot,
  );
  if (!store) return false;
  const info = snapshot.stores.find((s) => s.name === store);
  return info ? info[capability] === true : false;
}

/**
 * Non-hook form, for code outside a React render (event handlers, patches).
 *
 * Same fail-closed contract. Prefer {@link useStoreCapability} in components
 * so the value re-renders when the payload arrives.
 */
export function storeHasCapability(
  store: string | undefined | null,
  capability: StoreCapability,
): boolean {
  if (!store) return false;
  const info = storeInfoStore
    .getSnapshot()
    .stores.find((s) => s.name === store);
  return info ? info[capability] === true : false;
}
