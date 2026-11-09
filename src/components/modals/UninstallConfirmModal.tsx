/**
 * UninstallConfirmModal — delete confirmation for an
 * installed Unifideck game, with optional Proton-prefix wipe.
 *
 * Ported from staging: rich body with a description, a
 * ToggleField for "also delete Proton prefix", a conditional
 * red warning when the toggle is on, and two custom DialogButtons
 * (Cancel + red Uninstall with trash icon). The parent receives
 * the toggle state via `onConfirm(deletePrefix)`.
 *
 * An Xbox PC save written as a game quits can stay on the device until the
 * next launch, in the prefix. So the warning counts any such save (I-040). The
 * user can still delete: only launching the game can upload it, because
 * Unifideck never pushes a save itself.
 */
import { FC, useEffect, useState } from "react";
import { ConfirmModal, DialogButton, ToggleField } from "@decky/ui";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { FaExclamationTriangle, FaTrash } from "react-icons/fa";
import { useRPCQuery } from "../../api";
import { rpcRoutes } from "../../api/rpc-routes";

interface Props {
  gameId: number;
  gameTitle: string;
  onConfirm: (deletePrefix: boolean) => Promise<void> | void;
  closeModal: () => void;
}

/** The red warning's body: counts saves that have not uploaded, when any. */
export function prefixWarningText(t: TFunction, unuploaded: number): string {
  return unuploaded > 0
    ? t("uninstallModal.unuploadedBody", { count: unuploaded })
    : t("uninstallModal.warningBody");
}

export const UninstallConfirmModal: FC<Props> = ({
  gameId,
  gameTitle,
  onConfirm,
  closeModal,
}) => {
  const { t } = useTranslation();
  const [deletePrefix, setDeletePrefix] = useState(false);
  const warning = useRPCQuery<[number], { unuploaded: number }>(
    rpcRoutes.getUninstallSaveWarning,
    [gameId],
    { enabled: gameId > 0 },
  );
  const unuploaded = warning.data?.unuploaded ?? 0;
  useEffect(() => {
    // The dialog keeps the general warning, which is still true.
    if (warning.error) {
      console.warn(
        "[UninstallConfirmModal] could not count unuploaded saves:",
        warning.error,
      );
    }
  }, [warning.error]);
  return (
    <>
      <style>{`
        .unifideck-uninstall-modal + div { display: none !important; }
        .DialogFooter { display: none !important; }
      `}</style>
      <ConfirmModal
        strTitle={t("uninstallModal.title")}
        strDescription=""
        bHideCloseIcon={false}
        onOK={closeModal}
        onCancel={closeModal}
      >
        <div
          className="unifideck-uninstall-modal"
          style={{ padding: "10px 0" }}
        >
          <div
            style={{
              marginBottom: 16,
              color: "#ccc",
              fontSize: 14,
              lineHeight: 1.5,
            }}
          >
            {t("uninstallModal.description", { title: gameTitle })}
          </div>
          <div
            style={{
              marginBottom: 12,
              padding: 12,
              backgroundColor: "rgba(0, 0, 0, 0.2)",
              borderRadius: 8,
            }}
          >
            <ToggleField
              label={t("uninstallModal.deleteProtonLabel")}
              description={t("uninstallModal.deleteProtonDescription")}
              checked={deletePrefix}
              onChange={setDeletePrefix}
            />
          </div>
          {deletePrefix && (
            <div
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: 10,
                padding: 12,
                backgroundColor: "rgba(239, 68, 68, 0.15)",
                borderRadius: 8,
                marginBottom: 16,
                border: "1px solid rgba(239, 68, 68, 0.3)",
              }}
            >
              <FaExclamationTriangle
                style={{ color: "#ef4444", marginTop: 2, flexShrink: 0 }}
              />
              <div style={{ color: "#fca5a5", fontSize: 13, lineHeight: 1.4 }}>
                <strong>{t("uninstallModal.warningTitle")}</strong>{" "}
                {prefixWarningText(t, unuploaded)}
              </div>
            </div>
          )}
          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
            <DialogButton onClick={closeModal} style={{ minWidth: 100 }}>
              {t("uninstallModal.cancel")}
            </DialogButton>
            <DialogButton
              onClick={async () => {
                closeModal();
                await onConfirm(deletePrefix);
              }}
              style={{
                minWidth: 100,
                color: "#ef4444",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                gap: 6,
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
