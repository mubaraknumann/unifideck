import { FC, useState } from "react";
import { useTranslation } from "react-i18next";
import { call } from "@decky/api";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  ToggleField,
  showModal,
} from "@decky/ui";

interface CleanupSectionProps {
  syncing: boolean;
  syncCooldown: boolean;
}

export const CleanupSection: FC<CleanupSectionProps> = ({
  syncing,
  syncCooldown,
}) => {
  const { t } = useTranslation();
  const [deleting, setDeleting] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [deleteFiles, setDeleteFiles] = useState(false);

  const handleDeleteAll = async () => {
    if (!showDeleteConfirm) {
      setShowDeleteConfirm(true);
      return;
    }

    setDeleting(true);

    try {
      const result = await call<
        { delete_files: boolean },
        {
          success: boolean;
          deleted_games: number;
          deleted_artwork: number;
          deleted_files_count: number;
          preserved_shortcuts: number;
          error?: string;
        }
      >("perform_full_cleanup", { delete_files: deleteFiles });

      setShowDeleteConfirm(false);
      setDeleteFiles(false);

      if (result.success) {
        showModal(
          <div>
            <h2>{t("cleanup.successTitle")}</h2>
            <p>
              {t("cleanup.successMessage", {
                games: result.deleted_games,
                artwork: result.deleted_artwork,
                files: result.deleted_files_count,
              })}
            </p>
          </div>,
        );
      } else {
        showModal(
          <div>
            <h2>{t("cleanup.errorTitle")}</h2>
            <p>{result.error || t("cleanup.genericError")}</p>
          </div>,
        );
      }
    } catch (error) {
      console.error("Error performing cleanup:", error);
      showModal(
        <div>
          <h2>{t("cleanup.errorTitle")}</h2>
          <p>{t("cleanup.genericError")}</p>
        </div>,
      );
    } finally {
      setDeleting(false);
    }
  };

  return (
    <PanelSection title={t("cleanup.title")}>
      {!showDeleteConfirm ? (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={handleDeleteAll}
            disabled={syncing || deleting || syncCooldown}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "2px",
                fontSize: "0.85em",
                padding: "2px",
              }}
            >
              {t("cleanup.deleteAll")}
            </div>
          </ButtonItem>
        </PanelSectionRow>
      ) : (
        <>
          <PanelSectionRow>
            <Field
              label={t("cleanup.warningTitle")}
              description={t("cleanup.warningDescription")}
            />
          </PanelSectionRow>

          <PanelSectionRow>
            <ToggleField
              label={t("cleanup.deleteFilesLabel")}
              checked={deleteFiles}
              onChange={(checked) => setDeleteFiles(checked)}
            />
          </PanelSectionRow>

          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={handleDeleteAll}
              disabled={deleting}
            >
              {deleting ? t("cleanup.deleting") : t("cleanup.confirmDelete")}
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() => {
                setShowDeleteConfirm(false);
                setDeleteFiles(false);
              }}
              disabled={deleting}
            >
              {t("cleanup.cancel")}
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
};
