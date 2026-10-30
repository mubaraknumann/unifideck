/**
 * CleanupSection — entrypoint for the destructive "wipe every
 * Unifideck shortcut, artwork, auth token, and cache" flow.
 *
 * Opens a Decky `ConfirmModal` (CleanupConfirmModal) carrying the
 * destructive-mode toggle. On confirm, hits `perform_full_cleanup`,
 * wipes Steam in-memory shortcut state for each removed app_id,
 * deletes the `[Unifideck] *` Steam Collections, and reports
 * accurate counts via a toast.
 */
import { FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  showModal,
} from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useRPCMutation } from "../../api/useRPC";
import { rpcRoutes } from "../../api/rpc-routes";
import { useToast } from "../../hooks/useToast";
import { toastText, headerText } from "../../services/toast-text";
import { deleteAllUnifideckCollections } from "../../lib/steam-bridge/collection-manager";
import { CleanupConfirmModal } from "../modals/CleanupConfirmModal";

interface CleanupResult {
  deleted_games: number;
  deleted_files_count: number;
  deleted_artwork_count: number;
  logged_out_count: number;
  deleted_stray_files_count: number;
  deleted_residual_count: number;
  deleted_app_ids?: number[];
}

export const CleanupSection: FC = () => {
  const { t } = useTranslation();
  const toast = useToast();
  const { mutate, loading, error } = useRPCMutation<[boolean], CleanupResult>(
    rpcRoutes.performFullCleanup,
  );

  const runCleanup = async (deleteFiles: boolean) => {
    // Feedback on confirm — the backend wipe can take several seconds.
    // Deferred a beat on purpose: ConfirmModal fires onOK → onConfirm
    // synchronously *during* its close animation, and Decky's toaster
    // swallows toasts raised mid-teardown (the later success toast only
    // survives because it fires after the await). Letting the modal fully
    // unmount first makes the "started" toast actually appear.
    setTimeout(
      () =>
        toast.info(
          headerText("toasts.header.fullReset"),
          toastText("toasts.cleanupStartedMessage"),
        ),
      400,
    );
    const result = await mutate(deleteFiles);
    if (!result) {
      console.error("[Cleanup] reset failed:", error);
      toast.error(
        headerText("toasts.header.fullReset"),
        toastText("toasts.deleteFailed"),
      );
      return;
    }
    if (result.deleted_app_ids?.length) {
      const apps = (
        window as unknown as {
          SteamClient?: { Apps?: { RemoveShortcut: (id: number) => void } };
        }
      ).SteamClient?.Apps;
      for (const id of result.deleted_app_ids) {
        // `deleted_app_ids` are the SIGNED 32-bit ids as stored in
        // shortcuts.vdf, but `RemoveShortcut` keys off the UNSIGNED id in
        // `appStore.m_mapApps` — passing the signed form matched nothing,
        // so this loop quietly did nothing and the tiles lingered until
        // the next Steam restart. Mirrors `orphan_scan._to_unsigned`.
        const unsigned = id < 0 ? id + 0x100000000 : id;
        try {
          apps?.RemoveShortcut(unsigned);
        } catch {
          /* best effort */
        }
      }
    }
    await deleteAllUnifideckCollections().catch((e) =>
      console.error("[Cleanup] delete collections failed", e),
    );
    // Tell the frontend its library is gone. Two mechanisms, two scopes:
    // the backend emits SHORTCUT_INSTALL_STATE_CHANGED per cleared game
    // (Downloads tab + per-app install flags), while this window event is
    // the existing "re-read the whole library" hook — LibraryContext,
    // library-filters, overview-enrichment and UnifiedLibraryView all
    // listen, so store tabs and counts drop to zero without a reload.
    // Safe after the collection delete: an empty library makes
    // `syncTab` delete leftover `[Unifideck]` collections, never create them.
    window.dispatchEvent(new CustomEvent("unifideck-sync-completed"));
    const totalTouched =
      result.deleted_games +
      result.deleted_artwork_count +
      result.logged_out_count +
      result.deleted_stray_files_count +
      result.deleted_files_count +
      (result.deleted_residual_count ?? 0);
    if (totalTouched === 0) {
      toast.info(
        headerText("toasts.header.fullReset"),
        toastText("toasts.cleanupNoop"),
      );
    } else {
      toast.success(
        headerText("toasts.header.fullReset"),
        toastText("toasts.cleanupSuccessfulMessage"),
      );
    }
  };

  const handleClick = () => {
    showModal(<CleanupConfirmModal onConfirm={runCleanup} />);
  };

  return (
    <PanelSection title={t("cleanup.title")}>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={handleClick} disabled={loading}>
          {loading ? t("cleanup.deleting") : t("cleanup.deleteAll")}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};
