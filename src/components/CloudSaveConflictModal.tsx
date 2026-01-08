/**
 * Cloud Save Conflict Modal Component
 * 
 * Displays when local and cloud saves differ, allowing user to choose
 * which version to use (Steam-like behavior).
 */

import { FC, useState } from "react";
import { call, toaster } from "@decky/api";
import {
    ConfirmModal,
    showModal,
    DialogButton,
} from "@decky/ui";
import { FaCloud, FaDesktop, FaExclamationTriangle } from "react-icons/fa";

/**
 * Conflict information from backend
 */
export interface CloudSaveConflict {
    has_conflict: boolean;
    is_fresh: boolean;
    local_timestamp: number;
    cloud_timestamp: number;
    local_newer: boolean;
}

interface CloudSaveConflictModalProps {
    store: string;
    gameId: string;
    gameName: string;
    conflict: CloudSaveConflict;
    onResolved: (action: "download" | "upload") => void;
    closeModal?: () => void;
}

/**
 * Format timestamp to readable date string
 */
const formatTimestamp = (timestamp: number): string => {
    if (!timestamp) return "Unknown";
    const date = new Date(timestamp * 1000);
    return date.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "numeric",
        minute: "2-digit",
    });
};

/**
 * Cloud Save Conflict Modal
 * 
 * Shows when local saves differ from cloud saves, letting user choose.
 */
export const CloudSaveConflictModal: FC<CloudSaveConflictModalProps> = ({
    store,
    gameId,
    gameName,
    conflict,
    onResolved,
    closeModal,
}) => {
    const [resolving, setResolving] = useState(false);

    const handleUseCloud = async () => {
        setResolving(true);
        try {
            await call<[string, string, boolean], { success: boolean }>(
                "resolve_cloud_save_conflict",
                store,
                gameId,
                true // use_cloud = true
            );
            toaster.toast({
                title: "Using Cloud Saves",
                body: `Downloading cloud saves for ${gameName}`,
                duration: 3000,
            });
            onResolved("download");
            closeModal?.();
        } catch (error) {
            console.error("[CloudSaveConflictModal] Error resolving:", error);
            toaster.toast({
                title: "Error",
                body: "Failed to resolve conflict",
                duration: 5000,
                critical: true,
            });
        }
        setResolving(false);
    };

    const handleUseLocal = async () => {
        setResolving(true);
        try {
            await call<[string, string, boolean], { success: boolean }>(
                "resolve_cloud_save_conflict",
                store,
                gameId,
                false // use_cloud = false
            );
            toaster.toast({
                title: "Using Local Saves",
                body: `Uploading local saves for ${gameName}`,
                duration: 3000,
            });
            onResolved("upload");
            closeModal?.();
        } catch (error) {
            console.error("[CloudSaveConflictModal] Error resolving:", error);
            toaster.toast({
                title: "Error",
                body: "Failed to resolve conflict",
                duration: 5000,
                critical: true,
            });
        }
        setResolving(false);
    };

    const localTime = formatTimestamp(conflict.local_timestamp);
    const cloudTime = formatTimestamp(conflict.cloud_timestamp);

    return (
        <ConfirmModal
            strTitle="Cloud Save Conflict"
            strDescription=""
            onOK={closeModal}
            onCancel={closeModal}
        >
            <div style={{ padding: "10px 0" }}>
                {/* Warning icon and message */}
                <div style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "10px",
                    marginBottom: "15px",
                    color: "#ffc107"
                }}>
                    <FaExclamationTriangle size={24} />
                    <span style={{ fontSize: "14px" }}>
                        Your local saves differ from cloud saves for <strong>{gameName}</strong>
                    </span>
                </div>

                {/* Comparison */}
                <div style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: "10px",
                    marginBottom: "20px",
                    backgroundColor: "rgba(255,255,255,0.05)",
                    padding: "15px",
                    borderRadius: "8px"
                }}>
                    {/* Local saves */}
                    <div style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between"
                    }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                            <FaDesktop size={16} />
                            <span>Local:</span>
                        </div>
                        <span style={{
                            color: conflict.local_newer ? "#4caf50" : "#888",
                            fontWeight: conflict.local_newer ? "bold" : "normal"
                        }}>
                            {localTime} {conflict.local_newer && "(newer)"}
                        </span>
                    </div>

                    {/* Cloud saves */}
                    <div style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between"
                    }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                            <FaCloud size={16} />
                            <span>Cloud:</span>
                        </div>
                        <span style={{
                            color: !conflict.local_newer ? "#4caf50" : "#888",
                            fontWeight: !conflict.local_newer ? "bold" : "normal"
                        }}>
                            {cloudTime} {!conflict.local_newer && "(newer)"}
                        </span>
                    </div>
                </div>

                {/* Action buttons */}
                <div style={{
                    display: "flex",
                    gap: "10px",
                    justifyContent: "center",
                    marginBottom: "15px"
                }}>
                    <DialogButton
                        onClick={handleUseCloud}
                        disabled={resolving}
                        style={{
                            minWidth: "140px",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            gap: "8px"
                        }}
                    >
                        <FaCloud /> Use Cloud
                    </DialogButton>

                    <DialogButton
                        onClick={handleUseLocal}
                        disabled={resolving}
                        style={{
                            minWidth: "140px",
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            gap: "8px"
                        }}
                    >
                        <FaDesktop /> Use Local
                    </DialogButton>
                </div>
            </div>
        </ConfirmModal>
    );
};

/**
 * Show the conflict modal
 * 
 * Usage:
 *   const result = await showCloudSaveConflictModal("epic", "game123", "Brotato", conflictInfo);
 *   // result is "download" or "upload"
 */
export const showCloudSaveConflictModal = (
    store: string,
    gameId: string,
    gameName: string,
    conflict: CloudSaveConflict
): Promise<"download" | "upload"> => {
    return new Promise((resolve) => {
        showModal(
            <CloudSaveConflictModal
                store={store}
                gameId={gameId}
                gameName={gameName}
                conflict={conflict}
                onResolved={resolve}
            />
        );
    });
};

export default CloudSaveConflictModal;
