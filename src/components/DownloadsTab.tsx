/**
 * Downloads Tab Component
 *
 * Displays the download queue with:
 * - Current download (active, with progress bar)
 * - Queued downloads (waiting)
 * - Recently completed downloads
 * - Cancel functionality
 */

import { FC, useState, useEffect, useRef } from "react";
import { call, toaster } from "@decky/api";
import { PanelSection, PanelSectionRow, Field } from "@decky/ui";

import type { DownloadQueueInfo } from "../types/downloads";

import { t } from "../i18n";
import DownloadItemRow from "./downloads/DownloadItemRow";

/**
 * Empty state display
 */
const EmptyState: FC<{ message: string }> = ({ message }) => (
  <div
    style={{
      textAlign: "center",
      padding: "20px",
      color: "#888",
      fontSize: "14px",
    }}
  >
    {message}
  </div>
);

/**
 * Main Downloads Tab Component
 */
export const DownloadsTab: FC = () => {
  const [queueInfo, setQueueInfo] = useState<DownloadQueueInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const pollIntervalRef = useRef<NodeJS.Timeout | null>(null);

  // Fetch queue info
  const fetchQueueInfo = async () => {
    try {
      console.log("[DownloadsTab] Fetching queue info...");
      const timeoutPromise = new Promise((_, reject) =>
        setTimeout(() => reject(new Error("Fetch queue timeout")), 5000),
      );

      const result = (await Promise.race([
        call<[], DownloadQueueInfo>("get_download_queue_info"),
        timeoutPromise,
      ])) as DownloadQueueInfo;

      console.log("[DownloadsTab] Queue info result:", result);
      if (result.success) {
        setQueueInfo(result);
      } else {
        console.error("[DownloadsTab] Queue info returned success=false");
      }
    } catch (error) {
      console.error("[DownloadsTab] Error fetching queue info:", error);
      // Set a default empty state on error
      setQueueInfo({
        success: true,
        current: null,
        queued: [],
        completed: [],
      });
    }
    setLoading(false);
  };

  // Start polling when component mounts
  useEffect(() => {
    fetchQueueInfo();

    // Poll every second for progress updates
    pollIntervalRef.current = setInterval(fetchQueueInfo, 1000);

    return () => {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
      }
    };
  }, []);

  // Handle cancel
  const handleCancel = async (downloadId: string) => {
    try {
      const result = await call<[string], { success: boolean; error?: string }>(
        "cancel_download_by_id",
        downloadId,
      );

      if (result.success) {
        toaster.toast({
          title: t("downloadsTab.toastDownloadCancelledTitle"),
          body: t("downloadsTab.toastDownloadCancelledBody"),
          duration: 3000,
        });
        fetchQueueInfo(); // Refresh immediately
      } else {
        toaster.toast({
          title: t("downloadsTab.toastCancelFailedTitle"),
          body: t("downloadsTab.toastCancelFailedBody", {
            error: t(result.error || "Unknown error"),
          }),
          duration: 5000,
          critical: true,
        });
      }
    } catch (error) {
      console.error("[DownloadsTab] Error cancelling download:", error);
    }
  };

  // Handle clear finished item
  const handleClear = async (downloadId: string) => {
    try {
      const result = await call<[string], { success: boolean; error?: string }>(
        "clear_finished_download",
        downloadId,
      );

      if (result.success) {
        fetchQueueInfo(); // Refresh to remove the item
      }
    } catch (error) {
      console.error("[DownloadsTab] Error clearing finished download:", error);
    }
  };

  if (loading) {
    return (
      <PanelSection title={t("downloadsTab.currentDownload")}>
        <PanelSectionRow>
          <Field label={t("downloadsTab.loadingLabel")}>
            <span style={{ color: "#888" }}>
              {t("downloadsTab.loadingMessage")}
            </span>
          </Field>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const current = queueInfo?.current;
  const queued = queueInfo?.queued || [];
  const finished = queueInfo?.finished || [];
  const hasActiveDownloads = current || queued.length > 0;

  return (
    <>
      {/* Current Download Section */}
      <PanelSection title={t("downloadsTab.currentDownload")}>
        {current ? (
          <DownloadItemRow item={current} onCancel={handleCancel} />
        ) : (
          <EmptyState message={t("downloadsTab.noActiveDownloads")} />
        )}
      </PanelSection>

      {/* Queued Downloads Section */}
      {queued.length > 0 && (
        <PanelSection
          title={t("downloadsTab.queuedDownloads", { count: queued.length })}
        >
          {queued.map((item) => (
            <DownloadItemRow
              key={item.id}
              item={item}
              onCancel={handleCancel}
            />
          ))}
        </PanelSection>
      )}

      {/* Recently Completed Section */}
      {finished.length > 0 && (
        <PanelSection title={t("downloadsTab.recentlyCompleted")}>
          {finished.slice(0, 5).map((item) => (
            <DownloadItemRow
              key={item.id}
              item={item}
              onCancel={() => {}}
              onClear={handleClear}
            />
          ))}
        </PanelSection>
      )}

      {/* Empty state when nothing anywhere */}
      {!hasActiveDownloads && finished.length === 0 && (
        <PanelSection>
          <EmptyState message={t("downloadsTab.noDownloads")} />
        </PanelSection>
      )}
    </>
  );
};

export default DownloadsTab;
