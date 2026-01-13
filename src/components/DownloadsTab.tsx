/**
 * Downloads Tab Component
 * 
 * Displays the download queue with:
 * - Current download (active, with progress bar)
 * - Queued downloads (waiting)
 * - Recently completed downloads
 * - Cancel functionality
 */

import React, { FC, useState, useEffect, useRef } from "react";
import { call, toaster } from "@decky/api";
import {
    PanelSection,
    PanelSectionRow,
    ButtonItem,
    Field,
    ProgressBarWithInfo,
    Focusable,
    DialogButton,
    showModal,
    ConfirmModal,
} from "@decky/ui";
import { FaTimes, FaDownload, FaCheck, FaExclamationTriangle } from "react-icons/fa";

import type {
    DownloadItem,
    DownloadQueueInfo,
    StorageLocationInfo,
    StorageLocationsResponse,
} from "../types/downloads";

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
                border: isCurrent ? "1px solid #1a9fff" : "1px solid #333",
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
                    <DialogButton
                        onClick={() => onClear(item.id)}
                        style={{
                            padding: "0",
                            width: "20px",
                            height: "20px",
                            minWidth: "auto",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            backgroundColor: "transparent",
                            color: "#666",
                        }}
                    >
                        <FaTimes size={10} />
                    </DialogButton>
                )}
            </div>

            {/* Cancel button on new line for active downloads */}
            {(item.status === "downloading" || item.status === "queued") && (
                <div style={{ marginBottom: "8px" }}>
                    <DialogButton
                        onClick={() => {
                            showModal(
                                <ConfirmModal
                                    strTitle="Confirm Cancellation"
                                    strDescription={`Are you sure you want to cancel the download for ${item.game_title}?`}
                                    strOKButtonText="Yes"
                                    strCancelButtonText="No"
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
                        <FaTimes size={10} style={{ marginRight: "4px" }} /> Cancel
                    </DialogButton>
                </div>
            )}

            {/* Progress section (only for downloading) */}
            {item.status === "downloading" && (
                <>
                    {/* Show phase-specific messages */}
                    {item.download_phase === "extracting" && (
                        <div style={{ fontSize: "12px", color: "#f59e0b", marginBottom: "8px" }}>
                            {item.phase_message || "Extracting game files..."}
                        </div>
                    )}
                    {item.download_phase === "verifying" && (
                        <div style={{ fontSize: "12px", color: "#4ade80", marginBottom: "8px" }}>
                            ✓ {item.phase_message || "Verifying installation..."}
                        </div>
                    )}

                    {/* Show "Preparing..." when no real progress yet */}
                    {item.progress_percent === 0 && item.downloaded_bytes === 0 && item.download_phase === "downloading" ? (
                        <div style={{ fontSize: "12px", color: "#888", fontStyle: "italic" }}>
                            Preparing download...
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
                    ) : (
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
                                        width: `${item.progress_percent}%`,
                                        height: "100%",
                                        backgroundColor: "#1a9fff",
                                        transition: "width 0.3s ease",
                                    }}
                                />
                            </div>

                            {/* Stats row */}
                            <div style={{ display: "flex", justifyContent: "space-between", fontSize: "12px", color: "#888" }}>
                                <span>{item.progress_percent.toFixed(1)}%</span>
                                <span>
                                    {formatBytes(item.downloaded_bytes)} / {formatBytes(item.total_bytes)}
                                </span>
                                <span>{item.speed_mbps.toFixed(1)} MB/s</span>
                                <span>ETA: {formatETA(item.eta_seconds)}</span>
                            </div>
                        </>
                    )}
                </>
            )}

            {/* Status badge for non-downloading items */}
            {item.status !== "downloading" && (
                <div style={{ display: "flex", alignItems: "center", fontSize: "12px", color: statusColors[item.status] }}>
                    {item.status === "queued" && <FaDownload size={10} style={{ marginRight: "4px" }} />}
                    {item.status === "completed" && <FaCheck size={10} style={{ marginRight: "4px" }} />}
                    {item.status === "error" && <FaExclamationTriangle size={10} style={{ marginRight: "4px" }} />}
                    <span style={{ textTransform: "capitalize" }}>{item.status}</span>
                    {item.error_message && (
                        <span style={{ marginLeft: "8px", color: "#888" }}>- {item.error_message}</span>
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
                    title: "Download Cancelled",
                    body: "The download has been removed from the queue.",
                    duration: 3000,
                });
                fetchQueueInfo(); // Refresh immediately
            } else {
                toaster.toast({
                    title: "Cancel Failed",
                    body: result.error || "Unknown error",
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
            <PanelSection title="DOWNLOADS">
                <PanelSectionRow>
                    <Field label="Loading...">
                        <span style={{ color: "#888" }}>Fetching download queue...</span>
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
            <PanelSection title="CURRENT DOWNLOAD">
                {current ? (
                    <DownloadItemRow item={current} isCurrent={true} onCancel={handleCancel} />
                ) : (
                    <EmptyState message="No active downloads" />
                )}
            </PanelSection>

            {/* Queued Downloads Section */}
            {queued.length > 0 && (
                <PanelSection title={`QUEUED (${queued.length})`}>
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
                <PanelSection title="RECENTLY COMPLETED">
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
                    <EmptyState message="No downloads. Install games from your library to see them here." />
                </PanelSection>
            )}
        </>
    );
};

export default DownloadsTab;
