/**
 * PluginUpdater — the plugin self-update entry point in QAM Settings.
 *
 * Deliberately thin: a header naming the installed version and a single
 * "Update" button. Everything that used to live here — the version
 * Dropdown, the install button, Release Notes, Check for Updates — moved
 * into {@link file://./../modals/PluginUpdateModal.tsx}, because four
 * stacked controls was the tallest section in the panel for the feature
 * users touch least often, and it pushed store/sync/language settings
 * below the fold.
 *
 * The button label carries the update hint (`Update (v0.7.5 available)`)
 * from `updateData.available`, which this query already fetches — so a
 * pending update is visible without opening anything. The persistent
 * marker lives on the plugin's icon and title instead (see
 * {@link file://./../PluginBadge.tsx}).
 *
 * Nothing is shared with the modal at runtime: `showModal` mounts onto
 * the MAIN window's modal manager and this panel dismounts the instant
 * the modal opens, so live props would be pointing at a dead parent.
 * The panel simply remounts and re-queries on the next QAM open — by
 * which time the modal's `force_check_plugin_update` has already warmed
 * the backend cache, so the hint is current for free.
 */
import { FC, useMemo } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  showModal,
} from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useRPCQuery } from "../../api/useRPC";
import { rpcRoutes } from "../../api/rpc-routes";
import { PluginUpdateModal } from "../modals/PluginUpdateModal";
import type { UpdateCheckResult } from "../../lib/plugin-update";

export const PluginUpdater: FC = () => {
  const { t } = useTranslation();

  const { data: updateData, loading: isLoading } = useRPCQuery<
    [],
    UpdateCheckResult
  >(rpcRoutes.checkPluginUpdate, []);

  const currentVersion = updateData?.current ?? "0.0.0";
  const currentBuildId = updateData?.current_build_id ?? null;

  // Render header title
  const sectionTitle = useMemo(() => {
    if (isLoading) {
      return `${t("updater.titleLoading", {
        defaultValue: "Checking version",
      })}...`;
    }
    const buildSuffix = currentBuildId ? ` (${currentBuildId})` : "";
    return `${t("updater.currentTitle", {
      defaultValue: "Current",
    })} - v${currentVersion}${buildSuffix}`;
  }, [currentVersion, currentBuildId, isLoading, t]);

  // `available && !latest` shouldn't happen, but falling through to the
  // plain label beats rendering "Update (vundefined available)".
  const buttonLabel = useMemo(() => {
    const latestVersion = updateData?.latest?.version;
    if (updateData?.available && latestVersion) {
      return t("updater.manageButtonAvailable", {
        version: latestVersion,
        defaultValue: `Update (v${latestVersion} available)`,
      });
    }
    return t("updater.manageButton", { defaultValue: "Update" });
  }, [updateData, t]);

  return (
    <PanelSection title={sectionTitle}>
      <PanelSectionRow>
        {/* Not disabled while loading: the header already reads
            "Checking version…", and greying out a lone button for a
            ~1s cache hit reads as broken. The modal has its own
            loading state. */}
        <ButtonItem
          layout="below"
          onClick={() => showModal(<PluginUpdateModal />)}
        >
          {buttonLabel}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};
