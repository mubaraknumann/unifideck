/**
 * Uninstall Confirmation Modal Component
 * 
 * Shows uninstall confirmation with option to also delete Proton prefix files.
 * When "Also delete Proton files" is enabled, shows warning about save data loss.
 */

import { FC, useState } from "react";
import {
    ConfirmModal,
    DialogButton,
    ToggleField,
} from "@decky/ui";
import { FaTrash, FaExclamationTriangle } from "react-icons/fa";

interface UninstallConfirmModalProps {
    gameTitle: string;
    onConfirm: (deletePrefix: boolean) => void;
    closeModal?: () => void;
}

export const UninstallConfirmModal: FC<UninstallConfirmModalProps> = ({
    gameTitle,
    onConfirm,
    closeModal,
}) => {
    const [deletePrefix, setDeletePrefix] = useState(false);

    return (
        <>
            {/* Hide the default Confirm/Cancel button row */}
            <style>{`
                .uninstall-modal + div { display: none !important; }
                .DialogFooter { display: none !important; }
            `}</style>
            <ConfirmModal
                strTitle="Confirm Uninstallation"
                strDescription=""
                bHideCloseIcon={false}
                onOK={closeModal}
                onCancel={closeModal}
            >
                <div className="uninstall-modal" style={{ padding: "10px 0" }}>
                    {/* Main description */}
                    <div style={{
                        marginBottom: "16px",
                        color: "#ccc",
                        fontSize: "14px",
                        lineHeight: "1.5"
                    }}>
                        Are you sure you want to uninstall <strong>{gameTitle}</strong>?
                    </div>

                    {/* Delete Proton files toggle */}
                    <div style={{
                        marginBottom: "12px",
                        padding: "12px",
                        backgroundColor: "rgba(0, 0, 0, 0.2)",
                        borderRadius: "8px",
                    }}>
                        <ToggleField
                            label="Also delete Proton files"
                            description="Removes Compatibility and User Files data"
                            checked={deletePrefix}
                            onChange={(checked) => setDeletePrefix(checked)}
                        />
                    </div>

                    {/* Warning when toggle is enabled */}
                    {deletePrefix && (
                        <div style={{
                            display: "flex",
                            alignItems: "flex-start",
                            gap: "10px",
                            padding: "12px",
                            backgroundColor: "rgba(239, 68, 68, 0.15)",
                            borderRadius: "8px",
                            marginBottom: "16px",
                            border: "1px solid rgba(239, 68, 68, 0.3)",
                        }}>
                            <FaExclamationTriangle style={{
                                color: "#ef4444",
                                marginTop: "2px",
                                flexShrink: 0,
                            }} />
                            <div style={{
                                color: "#fca5a5",
                                fontSize: "13px",
                                lineHeight: "1.4"
                            }}>
                                <strong>Warning:</strong> This will permanently delete save data
                                stored in the Wine prefix. Make sure they are backed up.
                            </div>
                        </div>
                    )}

                    {/* Action buttons */}
                    <div style={{
                        display: "flex",
                        gap: "10px",
                        justifyContent: "flex-end",
                    }}>
                        <DialogButton
                            onClick={closeModal}
                            style={{
                                minWidth: "100px",
                            }}
                        >
                            Cancel
                        </DialogButton>
                        <DialogButton
                            onClick={() => {
                                closeModal?.();
                                onConfirm(deletePrefix);
                            }}
                            style={{
                                minWidth: "100px",
                                color: "#ef4444",
                                display: "flex",
                                alignItems: "center",
                                justifyContent: "center",
                                gap: "6px",
                            }}
                        >
                            <FaTrash /> Uninstall
                        </DialogButton>
                    </div>
                </div>
            </ConfirmModal>
        </>
    );
};

export default UninstallConfirmModal;
