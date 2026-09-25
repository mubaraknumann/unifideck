/**
 * useAppRunning — whether Steam considers a shortcut running right now.
 *
 * Polls Steam's per-client ``display_status`` every 2 s (4 = running,
 * 1 = launching). Shared by every Play-section variant that switches to a
 * Resume / Stop pair while its game runs: installed games and browser
 * games (xCloud, itch.io HTML5). It was private to ``InstalledButtons``
 * until the browser-game button needed the same answer.
 */
import { useEffect, useState } from "react";

const RUNNING_POLL_MS = 2000;
const STEAM_STATUS_RUNNING = 4;
const STEAM_STATUS_LAUNCHING = 1;

function readDisplayStatus(appId: number): number | undefined {
  const store = (
    window as unknown as {
      appStore?: {
        m_mapApps?: {
          get?: (
            id: number,
          ) =>
            | { local_per_client_data?: { display_status?: number } }
            | undefined;
        };
      };
    }
  ).appStore;
  const app = store?.m_mapApps?.get?.(appId);
  return app?.local_per_client_data?.display_status;
}

export function useAppRunning(appId: number): boolean {
  const [isRunning, setIsRunning] = useState(false);
  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      const status = readDisplayStatus(appId);
      if (status === undefined) return;
      setIsRunning(
        status === STEAM_STATUS_RUNNING || status === STEAM_STATUS_LAUNCHING,
      );
    };
    tick();
    const id = window.setInterval(tick, RUNNING_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [appId]);
  return isRunning;
}
