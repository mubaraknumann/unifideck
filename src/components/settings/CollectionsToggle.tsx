/**
 * CollectionsToggle — settings-tab section under the "STEAM COLLECTIONS"
 * heading. Hosts two independent toggles that happen to share this
 * heading:
 *
 *  - Steam Collections opt-in for the `[Unifideck]` collections. Default
 *    OFF: most users get the in-library tabs (which sync nowhere), and
 *    only those who want store collections mirrored into Desktop mode /
 *    their other Steam devices turn this on. Reads/writes via
 *    `useCollectionsEnabled`, so toggling takes effect live — turning it
 *    off deletes the collections immediately.
 *  - "Group duplicates" for the "All Games" / "Great on Deck" /
 *    "Installed" native tabs — collapse a title's cross-store copies to
 *    one tile (on) or show every store's copy separately (off, the
 *    default). Reads/writes via `useGroupDuplicatesEnabled`; the native
 *    tab filters read the same setting live, and `tab-container`
 *    rebuilds the tabs on the same change event, so this also takes
 *    effect without a restart.
 */
import { FC } from "react";
import { PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useCollectionsEnabled } from "../../hooks/useCollectionsEnabled";
import { useGroupDuplicatesEnabled } from "../../hooks/useGroupDuplicatesEnabled";

export const CollectionsToggle: FC = () => {
  const { t } = useTranslation();
  const { enabled, setEnabled } = useCollectionsEnabled();
  const { enabled: groupDuplicates, setEnabled: setGroupDuplicates } =
    useGroupDuplicatesEnabled();
  return (
    <PanelSection title={t("collectionSettings.title")}>
      <PanelSectionRow>
        <ToggleField
          label={
            enabled
              ? t("collectionSettings.enabled")
              : t("collectionSettings.disabled")
          }
          checked={enabled}
          onChange={setEnabled}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label={t("collectionSettings.groupDuplicates")}
          description={t("collectionSettings.groupDuplicatesDescription")}
          checked={groupDuplicates}
          onChange={setGroupDuplicates}
        />
      </PanelSectionRow>
    </PanelSection>
  );
};
