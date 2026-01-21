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
import {
    PanelSection,
    PanelSectionRow,
    Field,
    DialogButton,
    showModal,
    ConfirmModal,
} from "@decky/ui";
import { FaTimes, FaDownload, FaCheck, FaExclamationTriangle } from "react-icons/fa";

import type {
    DownloadItem,
    DownloadQueueInfo,
} from "../types/downloads";

import { t } from "../i18n";

/**
 * Format bytes to human-readable size
 */
function formatBytes(bytes: number): string {
    if (bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
}

/**
 * Format seconds to HH:MM:SS
 */
function formatETA(seconds: number): string {
    if (seconds <= 0) return "--:--";
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    if (hrs > 0) {
        return `${hrs}:${mins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
    }
    return `${mins}:${secs.toString().padStart(2, "0")}`;
}

/**
 * Store icon based on store type
 */
const StoreIcon: FC<{ store: string }> = ({ store }) => {
    const color = store === "epic" ? "#0078f2" : store === "amazon" ? "#FF9900" : "#a855f7";
    return (
        <span
            style={{
                display: "inline-block",
                width: "8px",
                height: "8px",
                borderRadius: "50%",
                backgroundColor: color,
                marginRight: "8px",
            }}
        />
    );
};

/**
 * Single download item display
 */
const DownloadItemRow: FC<{
    item: DownloadItem;
    isCurrent: boolean;
    onCancel: (id: string) => void;
    onClear?: (id: string) => void;
}> = ({ item, isCurrent, onCancel, onClear }) => {
    const statusColors: Record<string, string> = {
        downloading: "#1a9fff",
        queued: "#888",
        completed: "#4ade80",
        cancelled: "#f59e0b",
        error: "#ef4444",
    };

    return (
        <div
            style={{
                backgroundColor: "#1e2329",
                borderRadius: "8px",
                padding: "12px",
                marginBottom: "8px",
                // border: isCurrent ? "1px solid #1a9fff" : "1px solid #333",
            }}
        >
            {/* Header row: Title + Store + Clear button for finished */}
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" }}>
                <div style={{ display: "flex", alignItems: "center", flex: 1 }}>
                    <StoreIcon store={item.store} />
                    <span style={{ fontWeight: "bold", color: "#fff", fontSize: "14px" }}>
                        {item.game_title}
                    </span>
                </div>

                {/* X button to clear finished items */}
                {(item.status === "completed" || item.status === "error" || item.status === "cancelled") && onClear && (
                    <>
                        <style>{`
                            .unifideck-clear-btn {
                                padding: 0 !important;
                                width: 24px !important;
                                height: 24px !important;
                                min-width: 24px !important;
                                display: flex !important;
                                align-items: center !important;
                                justify-content: center !important;
                                background-color: transparent !important;
                                color: #888 !important;
                                border-radius: 4px !important;
                                transition: all 0.15s ease !important;
                            }
                            .unifideck-clear-btn:hover,
                            .unifideck-clear-btn:focus,
                            .unifideck-clear-btn.gpfocus,
                            .unifideck-clear-btn.Focusable:focus-within {
                                background-color: rgba(255, 255, 255, 0.15) !important;
                                color: #fff !important;
                                outline: 2px solid #1a9fff !important;
                                outline-offset: 1px !important;
                            }
                        `}</style>
                        <DialogButton
                            className="unifideck-clear-btn"
                            onClick={() => onClear(item.id)}
                            onOKButton={() => onClear(item.id)}
                        >
                            <FaTimes size={12} />
                        </DialogButton>
                    </>
                )}
            </div>

            {/* Cancel button on new line for active downloads */}
            {(item.status === "downloading" || item.status === "queued") && (
                <div style={{ marginBottom: "8px" }}>
                    <DialogButton
                        onClick={() => {
                            showModal(
                                <ConfirmModal
                                    strTitle={t("downloadsTab.confirmCancelTitle")}
                                    strDescription={t("downloadsTab.confirmCancelDescription", { game: item.game_title })}
                                    strOKButtonText={t("downloadsTab.confirmCancelYes")}
                                    strCancelButtonText={t("downloadsTab.confirmCancelNo")}
                                    bDestructiveWarning={true}
                                    onOK={() => onCancel(item.id)}
                                />
                            );
                        }}
                        style={{
                            padding: "4px 12px",
                            minWidth: "auto",
                            backgroundColor: "rgba(239, 68, 68, 0.2)",
                            color: "#ef4444",
                            fontSize: "12px",
                        }}
                    >
                        <FaTimes size={10} style={{ marginRight: "4px" }} /> {t("downloadsTab.confirmCancelNo")}
                    </DialogButton>
                </div>
            )}

            {/* Progress section (only for downloading) */}
            {item.status === "downloading" && (
                <>
                    {/* Show phase-specific messages */}
                    {item.download_phase === "extracting" && (
                        <div style={{ fontSize: "12px", color: "#f59e0b", marginBottom: "8px" }}>
                            {item.phase_message || t("downloadsTab.phaseExtracting", { game: item.game_title })}
                        </div>
                    )}
                    {item.download_phase === "verifying" && (
                        <div style={{ fontSize: "12px", color: "#4ade80", marginBottom: "8px" }}>
                            ✓ {item.phase_message || t("downloadsTab.phaseVerifying", { game: item.game_title })}
                        </div>
                    )}

                    {/* Show "Preparing..." when no real progress yet */}
                    {item.progress_percent === 0 && item.downloaded_bytes === 0 && item.download_phase === "downloading" ? (
                        <div style={{ fontSize: "12px", color: "#888", fontStyle: "italic" }}>
                            {t("downloadsTab.preparingDownload")}
                        </div>
                    ) : item.download_phase === "extracting" || item.download_phase === "verifying" ? (
                        /* Animated indeterminate progress bar for extraction/verification */
                        <div
                            style={{
                                width: "100%",
                                height: "6px",
                                backgroundColor: "#333",
                                borderRadius: "3px",
                                overflow: "hidden",
                                marginBottom: "8px",
                            }}
                        >
                            <div
                                style={{
                                    width: "30%",
                                    height: "100%",
                                    backgroundColor: item.download_phase === "extracting" ? "#f59e0b" : "#4ade80",
                                    animation: "slide 1.5s ease-in-out infinite",
                                }}
                            />
                            <style>{`
                                @keyframes slide {
                                    0% { transform: translateX(-100%); }
                                    100% { transform: translateX(400%); }
                                }
                            `}</style>
                        </div>
                    ) : (() => {
                        // Calculate progress from bytes when available (more accurate than chunk-based %)
                        // Legendary reports chunk-based progress which can differ significantly from byte progress
                        const byteProgress = item.total_bytes > 0
                            ? (item.downloaded_bytes / item.total_bytes) * 100
                            : item.progress_percent;
                        const displayProgress = Math.min(byteProgress, 100);

                        return (
                            <>
                                {/* Progress bar for downloading */}
                                <div
                                    style={{
                                        width: "100%",
                                        height: "6px",
                                        backgroundColor: "#333",
                                        borderRadius: "3px",
                                        overflow: "hidden",
                                        marginBottom: "8px",
                                    }}
                                >
                                    <div
                                        style={{
                                            width: `${displayProgress}%`,
                                            height: "100%",
                                            backgroundColor: "#1a9fff",
                                            transition: "width 0.3s ease",
                                        }}
                                    />
                                </div>

                                {/* Stats row */}
                                <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: "4px 8px", fontSize: "12px", color: "#888" }}>
                                    <span>{displayProgress.toFixed(1)}%</span>
                                    <span>
                                        {formatBytes(item.downloaded_bytes)} / {formatBytes(item.total_bytes)}
                                    </span>
                                    <span>{item.speed_mbps.toFixed(1)} MB/s</span>
                                    <span>ETA: {formatETA(item.eta_seconds)}</span>
                                </div>
                            </>
                        );
                    })()}
                </>
            )}

            {/* Status badge for non-downloading items */}
            {item.status !== "downloading" && (
                <div style={{ display: "flex", alignItems: "center", fontSize: "12px", color: statusColors[item.status] }}>
                    {item.status === "queued" && <FaDownload size={10} style={{ marginRight: "4px" }} />}
                    {item.status === "completed" && <FaCheck size={10} style={{ marginRight: "4px" }} />}
                    {item.status === "error" && <FaExclamationTriangle size={10} style={{ marginRight: "4px" }} />}
                    <span style={{ textTransform: "capitalize" }}>{t(`downloadsTab.status.${item.status}`)}</span>
                    {item.error_message && (
                        <span style={{ marginLeft: "8px", color: "#888" }}>- {t(item.error_message)}</span>
                    )}
                </div>
            )}
        </div>
    );
};

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
            const result = await call<[], DownloadQueueInfo>("get_download_queue_info");
            if (result.success) {
                setQueueInfo(result);
            }
        } catch (error) {
            console.error("[DownloadsTab] Error fetching queue info:", error);
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
                downloadId
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
                    body: t("downloadsTab.toastCancelFailedBody", { error: t(result.error || "Unknown error") }),
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
                downloadId
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
                        <span style={{ color: "#888" }}>{t("downloadsTab.loadingMessage")}</span>
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
                    <DownloadItemRow item={current} isCurrent={true} onCancel={handleCancel} />
                ) : (
                    <EmptyState message={t("downloadsTab.noActiveDownloads")} />
                )}
            </PanelSection>

            {/* Queued Downloads Section */}
            {queued.length > 0 && (
                <PanelSection title={t("downloadsTab.queuedDownloads", { count: queued.length })}>
                    {queued.map((item) => (
                        <DownloadItemRow
                            key={item.id}
                            item={item}
                            isCurrent={false}
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
                            isCurrent={false}
                            onCancel={() => { }}
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
