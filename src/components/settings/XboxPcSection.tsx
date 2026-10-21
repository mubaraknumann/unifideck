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
 */
import { FC, useEffect, useState } from "react";
import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  ToggleField,
} from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useXboxPcStatus } from "../../hooks/useXboxPcStatus";
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
          checked={status.enabled}
          disabled={busy}
          onChange={(on: boolean) => void onToggle(on)}
        />
      </PanelSectionRow>
    </PanelSection>
  );
};
