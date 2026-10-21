/**
 * XboxPcRuntimeRow: the Xbox PC components (our Xodus build and runtime).
 *
 * Shows the install state, live progress while installing (an
 * indeterminate bar while the size is unknown), and one action: Install,
 * Cancel, Retry or Update. A failure names its cause; a missing source
 * names the folder it looked in (rule 8). A runtime that installed without
 * some features (`degraded`) says which ones, because the user loses them.
 *
 * Used in the QAM section and in the setup modal.
 */
import { FC, useState } from "react";
import {
  ButtonItem,
  Field,
  PanelSectionRow,
  ProgressBarWithInfo,
} from "@decky/ui";
import { useTranslation } from "react-i18next";
import { xboxPcErrorText } from "../../lib/xbox-pc-errors";
import { xboxPcStore } from "../../stores/xbox-pc-store";
import { toastXboxPcError } from "../../services/store/xbox-pc-actions";
import { formatBytes } from "../../utils/format";
import type { XboxPcStatus } from "../../types/api";

/** Progress stage → key. Unknown stages read as plain "Working". */
const STAGE_KEYS: Readonly<Record<string, string>> = {
  checking: "xboxPc.runtime.stages.checking",
  copying_backend: "xboxPc.runtime.stages.copying",
  verifying: "xboxPc.runtime.stages.verifying",
  extracting: "xboxPc.runtime.stages.extracting",
  finalizing: "xboxPc.runtime.stages.finalizing",
};

/** Missing feature → key. */
const DEGRADED_KEYS: Readonly<Record<string, string>> = {
  cloud_saves: "xboxPc.runtime.degradedCloudSaves",
  xbox_sign_in_in_game: "xboxPc.runtime.degradedInGameSignIn",
};

/** The one action each state offers. */
type Action = "install" | "cancel" | "retry" | "update";

const ACTION_FOR: Readonly<Record<string, Action | undefined>> = {
  missing: "install",
  installing: "cancel",
  failed: "retry",
  outdated: "update",
};

const ACTION_KEYS: Readonly<Record<Action, string>> = {
  install: "xboxPc.runtime.install",
  cancel: "xboxPc.runtime.cancel",
  retry: "xboxPc.runtime.retry",
  update: "xboxPc.runtime.update",
};

const STATE_KEYS: Readonly<Record<string, string>> = {
  missing: "xboxPc.runtime.missing",
  installing: "xboxPc.runtime.installing",
  installed: "xboxPc.runtime.installed",
  outdated: "xboxPc.runtime.outdated",
  failed: "xboxPc.runtime.failed",
};

const errorStyle = { color: "#ff6b6b", fontSize: 12, lineHeight: 1.4 };
const noteStyle = { color: "#ffb74d", fontSize: 12, lineHeight: 1.4 };

async function runAction(action: Action): Promise<void> {
  if (action === "cancel") {
    const r = await xboxPcStore.cancelRuntimeInstall();
    if (!r.ok) toastXboxPcError("toasts.xboxPcRuntimeFailed", r.error);
    return;
  }
  const r = await xboxPcStore.installRuntime();
  if (!r.ok) toastXboxPcError("toasts.xboxPcRuntimeFailed", r.error);
}

/** The progress bar, while an install runs. */
const RuntimeProgress: FC<{ status: XboxPcStatus }> = ({ status }) => {
  const { t } = useTranslation();
  const p = status.runtime.progress;
  if (!p) return null;
  const total = p.total_bytes;
  const pct = total ? Math.min(100, (p.done_bytes / total) * 100) : 0;
  const stage = t(STAGE_KEYS[p.stage] ?? "common.working");
  const text = total
    ? `${stage} ${formatBytes(p.done_bytes)} / ${formatBytes(total)}`
    : stage;
  return (
    <PanelSectionRow>
      <ProgressBarWithInfo
        layout="inline"
        bottomSeparator="none"
        indeterminate={!total}
        nProgress={pct}
        sOperationText={text}
      />
    </PanelSectionRow>
  );
};

export const XboxPcRuntimeRow: FC<{ status: XboxPcStatus }> = ({ status }) => {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const rt = status.runtime;
  const action = ACTION_FOR[rt.state];
  const degraded = rt.degraded ?? [];
  const showError = rt.error && rt.state !== "installed";

  const onAction = async (): Promise<void> => {
    if (!action) return;
    setBusy(true);
    try {
      await runAction(action);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PanelSectionRow>
        <Field
          label={t("xboxPc.runtime.label")}
          description={t(STATE_KEYS[rt.state] ?? "xboxPc.runtime.missing", {
            version: rt.version ?? "",
          })}
          bottomSeparator="none"
        />
      </PanelSectionRow>
      {showError && (
        <PanelSectionRow>
          <div style={errorStyle}>
            {xboxPcErrorText(rt.error, t, rt.source_dir)}
          </div>
        </PanelSectionRow>
      )}
      {degraded.length > 0 && (
        <PanelSectionRow>
          <div style={noteStyle}>
            {degraded
              .map((d) =>
                DEGRADED_KEYS[d]
                  ? t(DEGRADED_KEYS[d])
                  : t("xboxPc.runtime.degradedOther", { feature: d }),
              )
              .join(" ")}
          </div>
        </PanelSectionRow>
      )}
      {rt.state === "installing" && <RuntimeProgress status={status} />}
      {action && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={() => void onAction()}
          >
            {t(ACTION_KEYS[action])}
          </ButtonItem>
        </PanelSectionRow>
      )}
    </>
  );
};
