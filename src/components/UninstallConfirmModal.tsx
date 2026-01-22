/**
 * Uninstall Confirmation Modal Component
 *
 * Shows uninstall confirmation with option to also delete Proton prefix files.
 * When "Also delete Proton files" is enabled, shows warning about save data loss.
 */

import { FC, useState } from "react";
import { ConfirmModal, DialogButton, ToggleField } from "@decky/ui";
import { FaTrash, FaExclamationTriangle } from "react-icons/fa";
import { useTranslation } from "react-i18next";

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
  const { t } = useTranslation();

  return (
    <>
      {/* Hide the default Confirm/Cancel button row */}
      <style>{`
                .uninstall-modal + div { display: none !important; }
                .DialogFooter { display: none !important; }
            `}</style>
      <ConfirmModal
        strTitle={t("uninstallModal.title")}
        strDescription=""
        bHideCloseIcon={false}
        onOK={closeModal}
        onCancel={closeModal}
      >
        <div className="uninstall-modal" style={{ padding: "10px 0" }}>
          {/* Main description */}
          <div
            style={{
              marginBottom: "16px",
              color: "#ccc",
              fontSize: "14px",
              lineHeight: "1.5",
            }}
          >
            {t("uninstallModal.description", { title: gameTitle })}
          </div>

          {/* Delete Proton files toggle */}
          <div
            style={{
              marginBottom: "12px",
              padding: "12px",
              backgroundColor: "rgba(0, 0, 0, 0.2)",
              borderRadius: "8px",
            }}
          >
            <ToggleField
              label={t("uninstallModal.deleteProtonLabel")}
              description={t("uninstallModal.deleteProtonDescription")}
              checked={deletePrefix}
              onChange={(checked) => setDeletePrefix(checked)}
            />
          </div>

          {/* Warning when toggle is enabled */}
          {deletePrefix && (
            <div
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: "10px",
                padding: "12px",
                backgroundColor: "rgba(239, 68, 68, 0.15)",
                borderRadius: "8px",
                marginBottom: "16px",
                border: "1px solid rgba(239, 68, 68, 0.3)",
              }}
            >
              <FaExclamationTriangle
                style={{
                  color: "#ef4444",
                  marginTop: "2px",
                  flexShrink: 0,
                }}
              />
              <div
                style={{
                  color: "#fca5a5",
                  fontSize: "13px",
                  lineHeight: "1.4",
                }}
              >
                <strong>{t("uninstallModal.warningTitle")}</strong>{" "}
                {t("uninstallModal.warningBody")}
              </div>
            </div>
          )}

          {/* Action buttons */}
          <div
            style={{
              display: "flex",
              gap: "10px",
              justifyContent: "flex-end",
            }}
          >
            <DialogButton
              onClick={closeModal}
              style={{
                minWidth: "100px",
              }}
            >
              {t("uninstallModal.cancel")}
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
              <FaTrash /> {t("uninstallModal.uninstall")}
            </DialogButton>
          </div>
        </div>
      </ConfirmModal>
    </>
  );
};

export default UninstallConfirmModal;
