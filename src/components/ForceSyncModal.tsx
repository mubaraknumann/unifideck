import { FC } from "react";
import { useTranslation } from "react-i18next";
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
    const { t } = useTranslation();

    return (
        <>
            {/* Hide the default Confirm/Cancel button row */}
            <style>{`
                .force-sync-modal + div { display: none !important; }
                .DialogFooter { display: none !important; }
            `}</style>
            <ConfirmModal
                strTitle={t('confirmModals.forceSyncTitle')}
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
                        {t('confirmModals.forceSyncDescription')}
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
                            <FaImage /> {t('confirmModals.resyncArtwork')}
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
                            <FaSync /> {t('confirmModals.keepArtwork')}
                        </DialogButton>
                    </div>
                </div>
            </ConfirmModal>
        </>
    );
};

export default ForceSyncModal;
