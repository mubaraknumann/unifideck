/**
 * useGroupDuplicatesEnabled — settings-tab toggle for whether cross-store
 * duplicate copies collapse to one tile in "All Games", "Great on Deck",
 * and "Installed".
 *
 * Thin React wrapper over the single source of truth in
 * `group-duplicates-setting` (localStorage + a broadcast event). The
 * native tab filters (module-level, outside React) read the same
 * setting live, and `tab-container` rebuilds the tabs on the same event
 * so the change takes effect without a Steam restart.
 */
import { useCallback, useEffect, useState } from "react";
import {
  GROUP_DUPLICATES_EVENT,
  isGroupDuplicatesEnabled,
  setGroupDuplicatesEnabled,
} from "../lib/group-duplicates-setting";

export interface UseGroupDuplicatesEnabledResult {
  enabled: boolean;
  setEnabled: (enabled: boolean) => void;
}

export function useGroupDuplicatesEnabled(): UseGroupDuplicatesEnabledResult {
  const [enabled, setEnabledState] = useState<boolean>(
    isGroupDuplicatesEnabled,
  );

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<boolean>).detail;
      if (typeof detail === "boolean") setEnabledState(detail);
    };
    window.addEventListener(GROUP_DUPLICATES_EVENT, handler);
    return () => window.removeEventListener(GROUP_DUPLICATES_EVENT, handler);
  }, []);

  const setEnabled = useCallback((next: boolean) => {
    setGroupDuplicatesEnabled(next);
    setEnabledState(next);
  }, []);

  return { enabled, setEnabled };
}
