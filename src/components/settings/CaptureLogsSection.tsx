/**
 * CaptureLogsSection — one-tap diagnostic bundle for bug reports.
 *
 * Calls `capture_logs`, which collects every plugin, launch and state
 * artifact from wherever it actually lives on the device, audits every
 * path the plugin can touch, probes the machine, scrubs credentials, and
 * writes one zip into the user's Downloads folder.
 *
 * The resulting path is surfaced twice on purpose: in a toast, and in a
 * row that stays in the panel. The user needs to read that path while
 * typing a bug report, long after a 5-second toast is gone.
 *
 * `lastCapture` is module-level rather than component state because
 * Decky tears the whole Quick Access panel down on dismiss — the same
 * reason QuickAccessPanel keeps its active tab outside React. Component
 * state alone would lose the path the moment the user closed the panel
 * to go find the file.
 */
import { FC, useRef, useState } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  Spinner,
  showModal,
} from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useRPC } from "../../api/useRPC";
import { rpcRoutes } from "../../api/rpc-routes";
import { useToast } from "../../hooks/useToast";
import { toastText, headerText } from "../../services/toast-text";
import { formatBytes, copyTextToClipboard } from "../../utils";
import { CaptureLogsConfirmModal } from "../modals/CaptureLogsConfirmModal";

/**
 * Payload of the `capture_logs` RPC.
 *
 * `skipped` and `errors` carry machine codes, not prose — a skipped
 * source is usually just a file this device does not have. Only
 * `errors` (a source that exists but could not be read) is worth
 * mentioning to the user; `audited` / `checks_failed` travel in the
 * payload for future use but are deliberately not displayed, because a
 * missing Amazon token on a device with no Amazon account is normal and
 * must not look like a problem.
 */
interface CaptureResult {
  archive_path: string;
  archive_name: string;
  bytes: number;
  file_count: number;
  dest_source: string;
  errors?: { key: string; error: string }[];
  skipped?: { key: string; reason: string }[];
  in_progress?: boolean;
}

/** Last successful capture, kept across QAM dismount/remount. */
let lastCapture: CaptureResult | null = null;

export const CaptureLogsSection: FC = () => {
  const { t } = useTranslation();
  const toast = useToast();
  const captureLogs = useRPC<[], CaptureResult>(rpcRoutes.captureLogs);
  const [result, setResult] = useState<CaptureResult | null>(lastCapture);
  const [busy, setBusy] = useState(false);
  // Authoritative re-entrancy latch. `busy` lags a render behind, and
  // `showModal` does not dedupe, so two stacked confirms could both
  // resolve before the first render commits — which would race two
  // captures on the same output filename.
  const runningRef = useRef(false);
  // Gives us the window that owns this panel. In Gaming Mode that is a
  // separate popup window, and a clipboard write has to target it.
  const rowRef = useRef<HTMLDivElement>(null);

  /**
   * Uses the low-level `useRPC` rather than `useRPCMutation` on purpose:
   * the mutation hook exposes its error via state read from the render
   * closure that created the handler, so on the *first* failure it is
   * still null and the message degrades to "unknown error". Catching the
   * thrown RpcError surfaces the backend's actual reason, which for the
   * one user-actionable failure here (nowhere writable to put the zip)
   * is the whole point of the toast.
   */
  const runCapture = async (): Promise<void> => {
    if (runningRef.current) return;
    runningRef.current = true;
    setBusy(true);
    // Deferred a beat: ConfirmModal fires onOK synchronously during its
    // close animation, and Decky's toaster swallows toasts raised
    // mid-teardown. Same workaround as CleanupSection.
    setTimeout(
      () =>
        toast.info(
          headerText("toasts.header.captureLogs"),
          toastText("toasts.captureLogsStartedMessage"),
        ),
      400,
    );
    try {
      const captured = await captureLogs();
      if (captured.in_progress) {
        toast.info(
          headerText("toasts.header.captureLogs"),
          toastText("toasts.captureLogsBusy"),
        );
        return;
      }
      lastCapture = captured;
      setResult(captured);
      // The path is in the section below the button; a toast has no room.
      const failures = captured.errors?.length ?? 0;
      if (failures > 0) {
        toast.show(
          headerText("toasts.header.captureLogs"),
          toastText("toasts.captureLogsPartialMessage"),
          { duration: 10000 },
        );
      } else {
        toast.success(
          headerText("toasts.header.captureLogs"),
          toastText("toasts.captureLogsDoneMessage", {
            size: formatBytes(captured.bytes),
          }),
        );
      }
    } catch (e) {
      console.error("[CaptureLogs] capture failed:", e);
      toast.error(
        headerText("toasts.header.captureLogs"),
        toastText("toasts.captureLogsFailed"),
      );
    } finally {
      runningRef.current = false;
      setBusy(false);
    }
  };

  const handleClick = (): void => {
    if (runningRef.current) return;
    showModal(<CaptureLogsConfirmModal onConfirm={() => void runCapture()} />);
  };

  const handleCopy = async (): Promise<void> => {
    if (!result) return;
    const ok = await copyTextToClipboard(
      result.archive_path,
      rowRef.current?.ownerDocument?.defaultView ?? null,
    );
    if (ok)
      toast.info(
        headerText("toasts.header.captureLogs"),
        toastText("toasts.captureLogsCopied"),
      );
    // A failed copy still shows the path in the section, so the user can
    // read it off there.
    else
      toast.error(
        headerText("toasts.header.captureLogs"),
        toastText("toasts.captureLogsCopyFailed"),
      );
  };

  const failureCount = result?.errors?.length ?? 0;

  return (
    <PanelSection title={t("captureLogs.title")}>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={handleClick}
          disabled={busy}
          description={t("captureLogs.description")}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 8,
            }}
          >
            {busy && <Spinner width={16} height={16} />}
            {busy ? t("captureLogs.capturing") : t("captureLogs.capture")}
          </div>
        </ButtonItem>
      </PanelSectionRow>
      {result && !busy && (
        <>
          <PanelSectionRow>
            <Field
              ref={rowRef}
              label={t("captureLogs.savedTo")}
              description={
                // dir="ltr" keeps path segments in order under an RTL
                // locale; break-all wraps rather than truncating,
                // because a path the user has to find is worth three
                // lines and useless with an ellipsis in it.
                <span
                  dir="ltr"
                  style={{
                    fontSize: 12,
                    lineHeight: "1.4",
                    wordBreak: "break-all",
                  }}
                >
                  {result.archive_path}
                </span>
              }
              childrenContainerWidth="fixed"
              bottomSeparator="none"
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field
              description={
                failureCount > 0
                  ? t("captureLogs.partialNote", { count: failureCount })
                  : t("captureLogs.bundleInfo", {
                      files: result.file_count,
                      size: formatBytes(result.bytes),
                    })
              }
              bottomSeparator="none"
            />
          </PanelSectionRow>
          {/* Rendered last deliberately: the gamepad only scrolls to
              focusable elements, and this section is the final one in
              the tab, so a focusable row below the path is what brings
              a below-the-fold path into view. */}
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => void handleCopy()}>
              {t("captureLogs.copyPath")}
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
};
