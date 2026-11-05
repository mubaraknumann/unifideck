/**
 * usePlayActions: every action a game page can trigger, in one place.
 *
 * Install, Play (with the pending-update prompt), Update, Cancel download,
 * Uninstall and browser-game Play. Steam's native action button (through
 * `NativeActionBinding`), the native gear menu's Uninstall item, and the
 * legacy custom play row all call these, so the prompts, toasts and error
 * mapping cannot drift between them.
 *
 * Needs `<DownloadProvider>` above it, like `useGameActions`.
 */
import { useCallback, useMemo } from "react";
import { ConfirmModal, showModal } from "@decky/ui";
import i18n from "i18next";
import { useGameInfo } from "./useGameInfo";
import { useGameActions, type UseGameActionsResult } from "./useGameActions";
import { useGameUpdate } from "./useGameUpdate";
import { useInstallFlow } from "./useInstallFlow";
import { useToast } from "./useToast";
import { useDownloads } from "../contexts/DownloadContext";
import { downloadErrorToast } from "../lib/download-errors";
import { toastText, headerText } from "../services/toast-text";
import { SteamBridge } from "../lib/steam-bridge";
import { launchAppWithControllerLayout } from "../utils/controllerConfig";
import { UninstallConfirmModal } from "../components/modals/UninstallConfirmModal";
import { UpdateAvailableModal } from "../components/modals/UpdateAvailableModal";
import type { DownloadItem } from "../types/downloads";

export interface CancelProgress {
  /** The user confirmed; the cancel is in flight. */
  onCancelling?: () => void;
  /** The cancel did not go through. */
  onFailed?: () => void;
}

export interface PlayActions {
  /** True while an install, update, uninstall or cancel is in flight. */
  isWorking: boolean;
  /** Pending store update known for this game. */
  hasUpdate: boolean;
  install: () => Promise<void>;
  /** Launch; asks first when an update is pending. */
  play: () => void;
  update: () => Promise<void>;
  /** Asks first, then cancels the given queue row. `progress` lets a
   *  caller show "Cancelling…" while it runs. */
  cancelDownload: (download: DownloadItem, progress?: CancelProgress) => void;
  /** Asks first (with the prefix toggle), then uninstalls. */
  uninstall: () => void;
  /** Opens a browser game with its controller layout. */
  playBrowser: (variant: "stream" | "web", url: string) => Promise<void>;
}

const defaultBridge = new SteamBridge();

/**
 * What confirming the uninstall dialog does: uninstall (optionally with
 * the Proton prefix) and say how it went. Shared by `usePlayActions` and
 * the native gear menu's Uninstall item.
 */
export function useUninstallConfirm(
  appId: number,
  bridge: SteamBridge = defaultBridge,
): (deletePrefix: boolean) => Promise<void> {
  const actions = useGameActions(bridge);
  const toast = useToast();
  return useCallback(
    async (deletePrefix: boolean) => {
      const r = await actions.uninstall(appId, deletePrefix);
      if (r.success) {
        toast.success(
          headerText("toasts.header.uninstall"),
          toastText("toasts.uninstallDone"),
        );
      } else {
        toast.error(
          headerText("toasts.header.uninstall"),
          downloadErrorToast(r.error),
        );
      }
    },
    [actions, appId, toast],
  );
}

/**
 * Cancel a download, asking first. Shared by the game page and the QAM, so a
 * stray press on either cannot throw away a download.
 *
 * Takes the caller's `actions`, so its `isWorking` covers the cancel.
 */
export function useCancelDownload(
  actions: Pick<UseGameActionsResult, "cancel">,
): (download: DownloadItem, progress?: CancelProgress) => void {
  const downloads = useDownloads();
  const toast = useToast();
  return useCallback(
    (download: DownloadItem, progress?: CancelProgress) => {
      const doCancel = async (): Promise<void> => {
        progress?.onCancelling?.();
        const result = await actions.cancel(download.id);
        if (result?.success) return;
        progress?.onFailed?.();
        // A cancel fails mainly because this row outlived its operation;
        // without a refetch nothing would ever replace it.
        void downloads.refresh();
        console.error("[usePlayActions] cancel failed:", result?.error);
        toast.error(
          headerText("toasts.header.download"),
          toastText("toasts.cancelFailed"),
        );
      };
      // A queued game has not started, so it gets its own wording.
      const queued = download.status === "queued";
      const title = download.game_title;
      showModal(
        <ConfirmModal
          strTitle={
            queued
              ? i18n.t("play.removeConfirmTitle")
              : i18n.t("play.cancelConfirmTitle")
          }
          strDescription={
            queued
              ? i18n.t("play.removeConfirmBody", { title })
              : i18n.t("play.cancelConfirmBody", { title })
          }
          strOKButtonText={
            queued
              ? i18n.t("downloads.removeFromQueue")
              : i18n.t("play.cancelConfirmConfirm")
          }
          strCancelButtonText={
            queued
              ? i18n.t("play.removeConfirmCancel")
              : i18n.t("play.cancelConfirmCancel")
          }
          bDestructiveWarning
          onOK={() => {
            void doCancel();
          }}
        />,
      );
    },
    [actions, downloads, toast],
  );
}

export function usePlayActions(
  appId: number,
  bridge: SteamBridge = defaultBridge,
): PlayActions {
  const { data: game } = useGameInfo(appId);
  const actions = useGameActions(bridge);
  const installFlow = useInstallFlow(bridge);
  const toast = useToast();
  const hasUpdate = useGameUpdate(game?.store, game?.id);

  const update = useCallback(async () => {
    if (!game) return;
    try {
      const res = await actions.update(appId, game.store, game.id);
      if (res?.success) {
        toast.success(
          headerText("toasts.header.update"),
          toastText("toasts.updateQueued"),
        );
      } else {
        toast.error(
          headerText("toasts.header.update"),
          downloadErrorToast(res?.error),
        );
      }
    } catch (e) {
      console.error("[usePlayActions] update failed:", e);
      toast.error(
        headerText("toasts.header.update"),
        downloadErrorToast(undefined),
      );
    }
  }, [actions, appId, game, toast]);

  // A pending update asks rather than blocks: most single-player titles run
  // fine a build behind, but an online game refuses to connect, and nothing
  // on the launch path would say why. Reads cached state only.
  const play = useCallback(() => {
    if (!game || !hasUpdate) {
      actions.launch(appId);
      return;
    }
    showModal(
      <UpdateAvailableModal
        gameTitle={game.title}
        onUpdate={update}
        onPlayAnyway={() => actions.launch(appId)}
        closeModal={() => {}}
      />,
    );
  }, [actions, appId, game, hasUpdate, update]);

  const install = useCallback(async () => {
    if (!game || installFlow.isWorking) return;
    const result = await installFlow.start(game);
    if (result == null) return;
    if (result.success) {
      toast.success(
        headerText("toasts.header.download"),
        toastText("toasts.downloadQueued"),
      );
    } else {
      toast.error(
        headerText("toasts.header.downloadGame", { title: game.title }),
        downloadErrorToast(result.error),
      );
    }
  }, [game, installFlow, toast]);

  const cancelDownload = useCancelDownload(actions);

  const confirmUninstall = useUninstallConfirm(appId, bridge);
  const uninstall = useCallback(() => {
    if (!game) return;
    showModal(
      <UninstallConfirmModal
        gameId={appId}
        gameTitle={game.title}
        onConfirm={confirmUninstall}
        closeModal={() => {}}
      />,
    );
  }, [appId, confirmUninstall, game]);

  const playBrowser = useCallback(
    async (variant: "stream" | "web", url: string) => {
      const launched = await launchAppWithControllerLayout(
        appId,
        variant === "stream" ? "gamepad" : "web-browser",
      );
      if (!launched && url) {
        // Steam's Apps surface is unavailable: open the game directly.
        window.open(`steam://openurl/${url}`, "_blank");
      }
    },
    [appId],
  );

  const isWorking = actions.isWorking || installFlow.isWorking;
  return useMemo(
    () => ({
      isWorking,
      hasUpdate,
      install,
      play,
      update,
      cancelDownload,
      uninstall,
      playBrowser,
    }),
    [
      isWorking,
      hasUpdate,
      install,
      play,
      update,
      cancelDownload,
      uninstall,
      playBrowser,
    ],
  );
}
