/**
 * NativeAppButtons: our extra icon button in Steam's `AppButtons` row.
 *
 * Steam's own controller and gear buttons stay as they are. After them
 * comes Uninstall (trash), for an installed game, so it is one press away
 * as it was in our old play row; the gear menu offers it too. It sits in
 * Steam's `MenuButtonContainer`, so it takes the same spacing and theme
 * rules as the buttons beside it. Cloud saves have their own status line
 * under the play bar (`NativeCloudStatusRow`).
 */
import { FC } from "react";
import { DialogButton } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaTrash } from "react-icons/fa";
import { DownloadProvider } from "../../contexts/DownloadContext";
import { usePlayActions } from "../../hooks/usePlayActions";
import { usePlaySection } from "../../hooks/usePlaySection";
import { getNativeClasses } from "../../lib/steam-bridge/native-app-details";
import { iconBtnClass, iconBtnStyle } from "../../components/play/PlayMeta";
import { PLAY_FOCUS_CSS } from "../../components/play/play.css";

const UninstallButton: FC<{ appId: number }> = ({ appId }) => {
  const { t } = useTranslation();
  const state = usePlaySection(appId);
  const actions = usePlayActions(appId);
  const installed =
    state.kind === "installed" ||
    (state.kind === "pc-unsupported" && state.installed);
  if (!installed) return null;
  return (
    <div className={getNativeClasses().playBar.MenuButtonContainer}>
      {/* Our icon-button rules (padding 0, 24 px glyph, focus states):
          without them Steam's DialogButton padding squeezes the glyph to
          0 px wide. Scoped to `unifideck-*`, inline so they land in this
          document. */}
      <style>{PLAY_FOCUS_CSS}</style>
      <DialogButton
        className={iconBtnClass()}
        style={iconBtnStyle}
        disabled={actions.isWorking}
        onClick={actions.uninstall}
        aria-label={t("play.uninstall")}
      >
        <FaTrash />
      </DialogButton>
    </div>
  );
};

export const NativeAppButtons: FC<{ appId: number }> = ({ appId }) => (
  <DownloadProvider>
    <UninstallButton appId={appId} />
  </DownloadProvider>
);
