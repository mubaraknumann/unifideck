/**
 * useStoreOwnershipEnabled: the "already owned" Steam Store ribbon toggle.
 *
 * Thin React wrapper over `lib/store-ownership-setting` (localStorage + a
 * broadcast event). The running ribbon module listens to the same event, so
 * flipping the toggle takes effect from the next store page.
 */
import { useCallback, useEffect, useState } from "react";
import {
  STORE_OWNERSHIP_ENABLED_EVENT,
  isStoreOwnershipEnabled,
  setStoreOwnershipEnabled,
} from "../lib/store-ownership-setting";

export interface UseStoreOwnershipEnabledResult {
  enabled: boolean;
  setEnabled: (enabled: boolean) => void;
}

export function useStoreOwnershipEnabled(): UseStoreOwnershipEnabledResult {
  const [enabled, setEnabledState] = useState<boolean>(isStoreOwnershipEnabled);

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<boolean>).detail;
      if (typeof detail === "boolean") setEnabledState(detail);
    };
    window.addEventListener(STORE_OWNERSHIP_ENABLED_EVENT, handler);
    return () =>
      window.removeEventListener(STORE_OWNERSHIP_ENABLED_EVENT, handler);
  }, []);

  const setEnabled = useCallback((next: boolean) => {
    setStoreOwnershipEnabled(next);
    setEnabledState(next);
  }, []);

  return { enabled, setEnabled };
}
