/**
 * useXboxPcStatus: the Xbox PC status snapshot, reactively.
 *
 * Reads the boot-time `xboxPcStore`, so mounting it costs no RPC. The
 * snapshot is `{status, loading, error}`: `status` is null until the first
 * answer, and `error` is set when none came (`status_unavailable`). Treat
 * an error as "unknown", never as "the feature is off".
 */
import { useSyncExternalStore } from "react";
import { xboxPcStore, type XboxPcSnapshot } from "../stores/xbox-pc-store";

/** Hook form. Re-renders whenever the store replaces its snapshot. */
export function useXboxPcStatus(): XboxPcSnapshot {
  return useSyncExternalStore(xboxPcStore.subscribe, xboxPcStore.getSnapshot);
}

/** Non-hook form, for code outside a render (the install flow). */
export function xboxPcSnapshot(): XboxPcSnapshot {
  return xboxPcStore.getSnapshot();
}
