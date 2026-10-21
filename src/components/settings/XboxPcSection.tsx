/**
 * XboxPcSection: the QAM section for Xbox PC games (experimental).
 *
 * Rendered after `<StoreConnections/>`. Hidden unless the backend reports
 * the feature as available on this build. When the status could not be
 * read it shows that, with a Retry, rather than hiding: a backend failure
 * must not look like the feature is off (rule 8).
 *
 * Turning it on goes through the unofficial notice (`enableXboxPc`).
 * Turning it off deletes nothing. While on, the section shows the
 * Microsoft sign-in it depends on, the components row, the Xbox PC
 * sign-in row and what the last library read found.
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
import { XboxPcRuntimeRow } from "./XboxPcRuntimeRow";
import { XboxPcAccountRow } from "./XboxPcAccountRow";
import type { XboxPcStatus } from "../../types/api";

const hintStyle = { fontSize: 12, lineHeight: 1.4, color: "#8f98a0" };

/** What the last library read found, once there was one. */
const LibraryLine: FC<{ status: XboxPcStatus }> = ({ status }) => {
  const { t } = useTranslation();
  const lib = status.library;
  if (!lib || lib.last_read == null) return null;
  return (
    <PanelSectionRow>
      <div style={hintStyle}>
        {t("xboxPc.section.libraryCounts", {
          installable: lib.installable,
          notSupported: lib.not_supported + lib.choice,
        })}
        {lib.stale ? ` ${t("xboxPc.section.libraryStale")}` : ""}
      </div>
    </PanelSectionRow>
  );
};

/** The error row: the status could not be read. */
const StatusError: FC<{ error: string }> = ({ error }) => {
  const { t } = useTranslation();
  return (
    <PanelSection title={t("xboxPc.section.title")}>
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
    <PanelSection title={t("xboxPc.section.title")}>
      <PanelSectionRow>
        <ToggleField
          label={t("xboxPc.section.toggleLabel")}
          description={t("xboxPc.section.toggleDescription")}
          checked={status.enabled}
          disabled={busy}
          onChange={(on: boolean) => void onToggle(on)}
        />
      </PanelSectionRow>
      {status.enabled && !status.xcloud_signed_in && (
        <PanelSectionRow>
          <div style={hintStyle}>{t("xboxPc.section.microsoftNeeded")}</div>
        </PanelSectionRow>
      )}
      {status.enabled && <XboxPcRuntimeRow status={status} />}
      {status.enabled && <XboxPcAccountRow status={status} />}
      {status.enabled && <LibraryLine status={status} />}
    </PanelSection>
  );
};
