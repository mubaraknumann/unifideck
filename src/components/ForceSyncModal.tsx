/**
 * Force Sync Options Modal Component
 * 
 * Shows two explicit options for force sync:
 * - Resync All Artwork (overwrites manual changes)
 * - Keep Current Artwork (only downloads missing)
 * 
 * Dismissing the modal (B button, outside click) does nothing.
 */

import { FC } from "react";
import {
    ConfirmModal,
    DialogButton,
} from "@decky/ui";
import { FaSync, FaImage } from "react-icons/fa";

interface ForceSyncModalProps {
    onResyncArtwork: () => void;
    onKeepArtwork: () => void;
    closeModal?: () => void;
}

export const ForceSyncModal: FC<ForceSyncModalProps> = ({
    onResyncArtwork,
    onKeepArtwork,
    closeModal,
}) => {
    return (
        <>
            {/* Hide the default Confirm/Cancel button row */}
            <style>{`
                .force-sync-modal + div { display: none !important; }
                .DialogFooter { display: none !important; }
            `}</style>
            <ConfirmModal
                strTitle="Force Sync Options"
                strDescription=""
                bHideCloseIcon={false}
                onOK={closeModal}
                onCancel={closeModal}
            >
                <div className="force-sync-modal" style={{ padding: "10px 0" }}>
                    {/* Description */}
                    <div style={{
                        marginBottom: "20px",
                        color: "#ccc",
                        fontSize: "14px",
                        lineHeight: "1.5"
                    }}>
                        Force Sync will rewrite all shortcuts and compatibility data.
                        Choose whether to resync all artwork or keep your current artwork.
                    </div>

                    {/* Action buttons */}
                    <div style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: "10px",
                    }}>
                        {/* Resync All Artwork - Destructive */}
                        <DialogButton
                            onClick={() => {
                                closeModal?.();
                                onResyncArtwork();
                            }}
                            style={{
                                display: "flex",
                                alignItems: "center",
                                justifyContent: "center",
                                gap: "8px",
                                color: "#ef4444",
                            }}
                        >
                            <FaImage /> Resync All Artwork
                        </DialogButton>

                        {/* Keep Current Artwork */}
                        <DialogButton
                            onClick={() => {
                                closeModal?.();
                                onKeepArtwork();
                            }}
                            style={{
                                display: "flex",
                                alignItems: "center",
                                justifyContent: "center",
                                gap: "8px",
                            }}
                        >
                            <FaSync /> Keep Current Artwork
                        </DialogButton>
                    </div>
                </div>
            </ConfirmModal>
        </>
    );
};

export default ForceSyncModal;
