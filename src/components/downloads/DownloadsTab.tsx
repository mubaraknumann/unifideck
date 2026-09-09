/**
 * DownloadsTab — download activity plus the installed-game list, inside the
 * QuickAccess panel.
 *
 * Four sections: "Now downloading", "Queued", "Installed", "Failed".
 *
 * The Installed section is derived from real install state
 * (``get_all_unifideck_games`` filtered on the backend's ``installed`` flag),
 * NOT from the download history. The tab used to show only the last handful
 * of downloads this plugin had performed, so games installed before the
 * plugin — or beyond the history cap — never appeared, and users resorted to
 * reinstalling a game just to make it show up. Deriving from install state
 * also means an uninstalled game simply leaves the list.
 *
 * Successful/cancelled history rows are gone with it: once the list reflects
 * what is actually installed, "this finished a while ago" is noise. Failures
 * stay, because a failure is the one outcome the user still needs to see —
 * and they can be dismissed once read.
 *
 * Live updates come from EventBus via `useDownloads()` for the queue, and
 * from SHORTCUT_INSTALL_STATE_CHANGED for the installed list.
 *
 * That event, specifically: it is the one the backend emits from
 * `ShortcutService.mark_installed` / `mark_uninstalled`, and it is what
 * *causes* the `installed` flag this list filters on
 * (`SyncService._on_shortcut_install_state_changed`), so a refetch on it is
 * guaranteed to see the new state. This used to listen for GAME_INSTALLED,
 * which had no runtime emitter at all, which is why a finished install never
 * appeared until the tab remounted. That event has since been removed
 * outright so nothing can subscribe to it again.
 */
import { FC, useCallback, useEffect, useMemo, useRef } from "react";
import {
  ButtonItem,
  Focusable,
  PanelSection,
  PanelSectionRow,
} from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useDownloads } from "../../contexts/DownloadContext";
import { useRPCQuery, useRPCMutation } from "../../api/useRPC";
import { rpcRoutes } from "../../api/rpc-routes";
import { useEventBus } from "../../api/event-bus-client";
import { Events } from "../../types/events";
import { DownloadItemRow } from "./DownloadItemRow";
import { InstalledGameRow } from "./InstalledGameRow";
import { PLAY_FOCUS_CSS } from "../play/play.css";
import type { Game } from "../../types/api";
import type { InstalledDiskInfoMap } from "../../types/downloads";

/** Trailing-edge window for coalescing install-state refetches. A sync or
 *  reconcile flips many games in a burst, and one EventBus poll dispatches
 *  every buffered record in a synchronous loop — without this, twenty
 *  flipped games mean twenty pairs of refetches, one of which walks install
 *  directories. Short enough to still feel immediate. */
const REFETCH_DEBOUNCE_MS = 300;

/**
 * Quick Access Menu tab: active downloads, the installed library, and any
 * failures worth surfacing.
 */
export const DownloadsTab: FC = () => {
  const { t } = useTranslation();
  const { queue, loading, refresh } = useDownloads();
  const games = useRPCQuery<[], Game[]>(rpcRoutes.getAllUnifideckGames, []);
  // Size + Internal/External per installed game. One bulk call rather than a
  // per-row `get_game_size_bytes`, because an installed size is an uncached
  // directory walk — see services/installed_disk_info.py.
  const diskInfo = useRPCQuery<[], InstalledDiskInfoMap>(
    rpcRoutes.getInstalledDiskInfo,
    [],
  );
  const clearHistory = useRPCMutation<[string | null], unknown>(
    rpcRoutes.clearDownloadHistory,
  );

  // Install state changes without a library sync, so refetch on the bus
  // events rather than waiting for `unifideck-sync-completed`.
  const refetchGames = useCallback(() => {
    void games.refetch();
    void diskInfo.refetch();
  }, [games, diskInfo]);

  // Debounced so an event burst costs one refetch pair. The callback is read
  // through a ref: `refetchGames` gets a new identity every render (both
  // query objects do), and re-creating the timer with it would either reset
  // the pending window or fire a stale closure.
  const refetchRef = useRef(refetchGames);
  refetchRef.current = refetchGames;
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scheduleRefetch = useCallback(() => {
    if (timerRef.current != null) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      refetchRef.current();
    }, REFETCH_DEBOUNCE_MS);
  }, []);
  useEffect(
    () => () => {
      if (timerRef.current != null) clearTimeout(timerRef.current);
    },
    [],
  );

  useEventBus(Events.SHORTCUT_INSTALL_STATE_CHANGED, scheduleRefetch, []);
  // Redundant with the above (an uninstall reaches `mark_uninstalled`, which
  // emits it) but harmless, and it keeps the list honest if a store ever
  // grows an uninstall path that skips the shortcut layer.
  useEventBus(Events.GAME_UNINSTALLED, scheduleRefetch, []);

  const installed = useMemo(() => {
    // Raw RPC rows carry the wire field `installed`; only adapter-normalised
    // rows have `is_installed`. Read both (see the note on `Game`).
    const rows = (games.data ?? []).filter(
      (g) => g.installed ?? g.is_installed,
    );
    return [...rows].sort((a, b) => a.title.localeCompare(b.title));
  }, [games.data]);

  if (loading || !queue) return null;
  // Defensive : backend may omit any of these keys on early
  // boot or partial-failure responses. Treat missing as empty.
  const current = queue.current ?? null;
  const queued = queue.queued ?? [];
  // Only failures survive in the history view now — completions are
  // represented by the game appearing in "Installed".
  const failed = (queue.finished ?? []).filter((i) => i.status === "failed");

  const empty =
    !current &&
    queued.length === 0 &&
    failed.length === 0 &&
    installed.length === 0;
  if (empty) {
    return (
      <PanelSection title={t("downloads.title")}>
        <div style={{ padding: 16, textAlign: "center", color: "#94a3b8" }}>
          {t("downloads.empty")}
        </div>
      </PanelSection>
    );
  }
  return (
    <>
      {/* Inline so the button/badge focus rules land in the QuickAccess
          CEF document (separate from the App-Details one). */}
      <style>{PLAY_FOCUS_CSS}</style>
      {current && (
        <PanelSection title={t("downloads.current")}>
          <DownloadItemRow item={current} variant="current" />
        </PanelSection>
      )}
      {queued.length > 0 && (
        <PanelSection title={t("downloads.queued")}>
          {queued.map((item) => (
            <DownloadItemRow key={item.id} item={item} variant="queued" />
          ))}
        </PanelSection>
      )}
      {installed.length > 0 && (
        <PanelSection
          title={t("downloads.installedCount", { count: installed.length })}
        >
          {/* ONE grid over the whole list, not one nav container per row.
              Steam resolves "grid" flow geometrically in 2-D, so DOWN from a
              Play button finds the Play button directly below it and DOWN
              from Uninstall finds the next Uninstall — the columns stay
              intact, and only LEFT/RIGHT crosses between them. Per-row
              containers gave the opposite: every vertical step re-entered a
              fresh container and landed on whichever button it felt like. */}
          <Focusable flow-children="grid">
            {installed.map((game) => (
              <InstalledGameRow
                key={`${game.store}:${game.id}`}
                game={game}
                // Keyed by STORE_GAME_ID, not `game.id` — the two are not
                // always the same string (same caveat as the appId lookup
                // inside the row).
                disk={diskInfo.data?.[`${game.store}:${game.store_game_id}`]}
                onUninstalled={refetchGames}
              />
            ))}
          </Focusable>
        </PanelSection>
      )}
      {failed.length > 0 && (
        <PanelSection title={t("downloads.failed")}>
          {failed.map((item) => (
            <DownloadItemRow key={item.id} item={item} variant="finished" />
          ))}
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={clearHistory.loading}
              // Clearing history emits no bus event, so pull the queue
              // snapshot back explicitly or the rows linger until the next
              // poll.
              onClick={() => {
                void clearHistory.mutate(null).then(refresh);
              }}
            >
              {t("downloads.clearFailed")}
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}
    </>
  );
};
