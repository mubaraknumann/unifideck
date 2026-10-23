/**
 * XboxPcSection: the one QAM toggle for Xbox PC downloads (experimental).
 *
 * Rendered after `<StoreConnections/>`. Hidden unless the backend reports
 * the feature as available on this build. When the status could not be
 * read it shows that, with a Retry, rather than hiding: a backend failure
 * must not look like the feature is off (rule 8).
 *
 * There is nothing else to set up by hand. Turning it on goes through the
 * unofficial notice once (`enableXboxPc`); after that, with Microsoft
 * connected in Store Connections, the components install and the Xbox PC
 * sign-in runs by themselves (`linkXboxPc`). Turning it off deletes nothing.
 * While it is on, a small line under the toggle shows the runtime version
 * or the download progress.
 */
import { FC, useEffect, useState } from "react";
import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  ToggleField,
} from "@decky/ui";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { useXboxPcStatus } from "../../hooks/useXboxPcStatus";
import type { XboxPcStatus } from "../../types/api";
import { xboxPcErrorText } from "../../lib/xbox-pc-errors";
import { xboxPcStore } from "../../stores/xbox-pc-store";
import {
  disableXboxPc,
  enableXboxPc,
} from "../../services/store/xbox-pc-actions";

/** The error row: the status could not be read. */
const StatusError: FC<{ error: string }> = ({ error }) => {
  const { t } = useTranslation();
  return (
    <PanelSection>
      <PanelSectionRow>
        <Field label={xboxPcErrorText(error, t)} bottomSeparator="none" />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => void xboxPcStore.refetch()}>
          {t("xboxPc.section.retry")}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};

/**
 * The small line under the toggle: the installed runtime version, the
 * download progress, or why the components are not installed.
 */
export function runtimeLine(
  runtime: XboxPcStatus["runtime"],
  t: TFunction,
): string | undefined {
  const { state, version, progress, error } = runtime;
  if (state === "installing") {
    if (progress?.stage === "downloading" && progress.total_bytes) {
      const percent = Math.floor(
        (progress.done_bytes / progress.total_bytes) * 100,
      );
      return t("xboxPc.section.runtimeDownloading", { percent });
    }
    return t("xboxPc.section.runtimeInstalling");
  }
  if ((state === "installed" || state === "outdated") && version) {
    const key =
      state === "outdated"
        ? "xboxPc.section.runtimeUpdateAvailable"
        : "xboxPc.section.runtimeVersion";
    return t(key, { version });
  }
  return error ? xboxPcErrorText(error, t, runtime.source_dir) : undefined;
}

export const XboxPcSection: FC = () => {
  const { t } = useTranslation();
  const { status, error } = useXboxPcStatus();
  const [busy, setBusy] = useState(false);

  // The QAM may open long after boot; re-read the cached status (cheap).
  useEffect(() => {
    void xboxPcStore.refetch();
  }, []);

  if (!status) return error ? <StatusError error={error} /> : null;
  if (!status.available) return null;

  const onToggle = async (on: boolean): Promise<void> => {
    setBusy(true);
    try {
      await (on ? enableXboxPc(status) : disableXboxPc());
    } finally {
      setBusy(false);
    }
  };

  return (
    <PanelSection>
      <PanelSectionRow>
        <ToggleField
          label={t("xboxPc.section.enableDownloads")}
          description={
            status.enabled ? runtimeLine(status.runtime, t) : undefined
          }
          checked={status.enabled}
          disabled={busy}
          onChange={(on: boolean) => void onToggle(on)}
        />
      </PanelSectionRow>
    </PanelSection>
  );
};
