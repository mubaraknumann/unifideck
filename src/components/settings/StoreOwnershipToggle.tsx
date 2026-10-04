/**
 * StoreOwnershipToggle: settings-tab section for the Steam Store ribbon
 * that marks games the user already owns on another store. Default ON.
 * Reads/writes via `useStoreOwnershipEnabled`, so the change applies from
 * the next store page without a restart.
 */
import { FC } from "react";
import { PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useStoreOwnershipEnabled } from "../../hooks/useStoreOwnershipEnabled";

export const StoreOwnershipToggle: FC = () => {
  const { t } = useTranslation();
  const { enabled, setEnabled } = useStoreOwnershipEnabled();
  return (
    <PanelSection title={t("storeOwnership.settingsTitle")}>
      <PanelSectionRow>
        <ToggleField
          label={t("storeOwnership.toggleLabel")}
          checked={enabled}
          onChange={setEnabled}
        />
      </PanelSectionRow>
    </PanelSection>
  );
};
