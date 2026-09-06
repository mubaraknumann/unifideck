/**
 * PluginUpdateModal — the whole plugin self-update surface, in a modal.
 *
 * The QAM Settings tab used to stack four controls for this (version
 * Dropdown, install, Release Notes, Check for Updates) — the tallest
 * section in the panel for the thing users touch least often. It now
 * sits behind one "Update" button in {@link file://./../settings/PluginUpdater.tsx}.
 *
 * Deliberately self-contained : its own RPC queries, its own progress
 * state, its own `loader/plugin_download_*` subscription. Nothing is
 * passed in from the panel and nothing is reported back, because the
 * panel is *gone* while this is open — verified against a live Gaming
 * Mode session: `showModal` mounts onto the MAIN window's modal manager
 * (not the QAM popup's), and the QAM panel dismounts the instant the
 * modal appears. The upside of the same fact is that the install can be
 * watched to completion even after the QAM is dismissed.
 *
 * Stacking is safe in both directions. Steam's modal manager keeps a
 * push/splice array and its overlay renders EVERY entry, marking only
 * the topmost `active` — a modal underneath stays mounted, so neither
 * `ReleaseNotesModal` nor Decky Loader's own install-confirm dialog
 * popping on top tears down the listeners feeding the progress bar.
 */
import { FC, useState, useEffect, useMemo, useRef } from "react";
import {
  ConfirmModal,
  Dropdown,
  DialogButton,
  Focusable,
  ProgressBarWithInfo,
  showModal,
} from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaDownload } from "react-icons/fa";
import { useRPCQuery, useRPCMutation } from "../../api/useRPC";
import { rpcRoutes } from "../../api/rpc-routes";
import { useToast } from "../../hooks/useToast";
import { ReleaseNotesModal } from "./ReleaseNotesModal";
import {
  INSTALL_TYPE_DOWNGRADE,
  INSTALL_TYPE_REINSTALL,
  INSTALL_WATCHDOG_TIMEOUT_MS,
  extractDevBuildId,
  getDeckyBackend,
  getPersistedSelectedTag,
  logEvent,
  resolveInstallAction,
  setPersistedSelectedTag,
  stageLabel,
  type ReleaseInfo,
  type UpdateCheckResult,
} from "../../lib/plugin-update";

interface Props {
  /** Injected by `showModal()`. */
  closeModal?: () => void;
}

export const PluginUpdateModal: FC<Props> = ({ closeModal }) => {
  const { t } = useTranslation();
  const toast = useToast();

  // The selected release is identified by its unique tag (not the parsed
  // version, which could collide between a stable and a prerelease).
  const [selectedTag, setSelectedTag] = useState<string>("");
  const [installing, setInstalling] = useState(false);
  const [checking, setChecking] = useState(false);

  // Live install progress mirrored from Decky's loader events.
  const [downloadActive, setDownloadActive] = useState(false);
  const [downloadPercent, setDownloadPercent] = useState(0);
  const [downloadStatus, setDownloadStatus] = useState("");
  const downloadActiveRef = useRef(false);
  const watchdogTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Fetch updates status
  const {
    data: updateData,
    loading: checkingOnMount,
    refetch: checkUpdate,
  } = useRPCQuery<[], UpdateCheckResult>(rpcRoutes.checkPluginUpdate, []);

  // Fetch available versions
  const {
    data: versionsData,
    loading: loadingVersions,
    refetch: refetchVersions,
  } = useRPCQuery<[], ReleaseInfo[]>(rpcRoutes.getAvailableVersions, []);

  // Cache-bypassing variants — used by the explicit "Check for Updates"
  // action and before installing a prerelease, whose single GitHub asset
  // gets deleted and re-uploaded under a new name/URL on every dev build.
  // The plain queries above stay on the 1-hour cache (mount-time auto-check,
  // background poller) so we don't hammer GitHub's unauthenticated rate limit.
  const forceCheckMut = useRPCMutation<[], UpdateCheckResult>(
    rpcRoutes.forceCheckPluginUpdate,
  );
  const forceVersionsMut = useRPCMutation<[], ReleaseInfo[]>(
    rpcRoutes.forceGetAvailableVersions,
  );

  const currentVersion = updateData?.current ?? "0.0.0";
  const currentBuildId = updateData?.current_build_id ?? null;
  const initializedRef = useRef(false);

  // Seed the selection when data loads. Restore a prior selection (persisted
  // across remounts) if it still resolves to an available release; otherwise
  // default to the installed version, then latest, then newest.
  useEffect(() => {
    if (updateData && versionsData && !initializedRef.current) {
      const persisted = getPersistedSelectedTag();
      if (persisted && versionsData.some((v) => v.tag === persisted)) {
        setSelectedTag(persisted);
      } else {
        // A prerelease row's `version` is always its raw non-semver tag
        // ("Dev-<date>-<time>-<sha>" — no semver can be parsed from it),
        // so it can never equal currentVersion. Match it via the build id
        // baked into its asset filename instead.
        //
        // currentBuildId !== null is also the deciding factor for the
        // stable-release branch below: dev builds are deliberately cut
        // BEFORE package.json's version gets bumped for release, so a
        // genuinely-installed dev build's `current` is identical to the
        // officially-tagged stable release sharing that same frozen
        // base version (e.g. both read "0.7.0"). Without this guard,
        // a real dev install would match the stable release's `version
        // === currentVersion` check too, seeding the selection (and
        // "(installed)" tag below) onto the wrong row.
        const current = versionsData.find((v) =>
          v.prerelease
            ? currentBuildId !== null &&
              extractDevBuildId(v.asset_name) === currentBuildId
            : currentBuildId === null && v.version === currentVersion,
        );
        const tag =
          current?.tag ?? updateData.latest?.tag ?? versionsData[0]?.tag ?? "";
        setPersistedSelectedTag(tag || null);
        setSelectedTag(tag);
      }
      initializedRef.current = true;
    }
  }, [updateData, versionsData, currentVersion, currentBuildId]);

  // Subscribe to Decky's loader install events to mirror progress in-modal.
  useEffect(() => {
    const backend = getDeckyBackend();
    if (!backend) return;

    const clearWatchdog = () => {
      if (watchdogTimerRef.current !== null) {
        clearTimeout(watchdogTimerRef.current);
        watchdogTimerRef.current = null;
      }
    };
    // Decky's own install code can die silently (confirmed: a 404 on a
    // rotated dev-build asset logs one CRITICAL line in journalctl and then
    // never fires plugin_download_info/finish again) — without this, the
    // modal freezes forever with downloadActive stuck true.
    const armWatchdog = () => {
      clearWatchdog();
      watchdogTimerRef.current = setTimeout(() => {
        if (!downloadActiveRef.current) return;
        downloadActiveRef.current = false;
        setDownloadActive(false);
        logEvent("error", "watchdog_timeout after 45s of inactivity");
        toast.error(
          t("updater.installFailedTitle", { defaultValue: "Install Failed" }),
          t("updater.installTimeoutMessage", {
            defaultValue:
              "No response from Decky Loader — the install may have stalled or failed. Please try again.",
          }),
        );
      }, INSTALL_WATCHDOG_TIMEOUT_MS);
    };

    const onStart = (name: string) => {
      if (name !== "Unifideck") return;
      downloadActiveRef.current = true;
      setDownloadActive(true);
      setDownloadPercent(0);
      setDownloadStatus(stageLabel("start", t));
      logEvent("download_start", name);
      armWatchdog();
    };
    const onInfo = (percent: number, key?: string) => {
      if (!downloadActiveRef.current) return;
      setDownloadPercent(percent);
      setDownloadStatus(stageLabel(key, t));
      logEvent("progress", `${percent}% ${key ?? ""}`.trim());
      armWatchdog();
    };
    const onFinish = (name: string) => {
      if (name !== "Unifideck") return;
      downloadActiveRef.current = false;
      setDownloadPercent(100);
      setDownloadActive(false);
      logEvent("download_finish", name);
      clearWatchdog();
    };

    backend.addEventListener("loader/plugin_download_start", onStart);
    backend.addEventListener("loader/plugin_download_info", onInfo);
    backend.addEventListener("loader/plugin_download_finish", onFinish);
    return () => {
      backend.removeEventListener("loader/plugin_download_start", onStart);
      backend.removeEventListener("loader/plugin_download_info", onInfo);
      backend.removeEventListener("loader/plugin_download_finish", onFinish);
      // Avoid a false-positive error toast firing after the modal closes
      // while a download legitimately continues server-side.
      clearWatchdog();
    };
    // toast.error is useCallback-memoized in useToast(), so this is stable
    // and won't cause the effect (and its addEventListener subscriptions)
    // to re-run on every render the way depending on `toast` itself would
    // (useToast() returns a new object literal every render).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t, toast.error]);

  const selectedRelease = useMemo(() => {
    if (!versionsData) return null;
    return versionsData.find((v) => v.tag === selectedTag) || null;
  }, [versionsData, selectedTag]);

  const selectedVersion = selectedRelease?.version ?? "";

  // Format dropdown options
  const versionOptions = useMemo(() => {
    if (!versionsData) return [];
    return versionsData.map((v) => {
      // A dev release's asset filename carries the real build identity
      // (branch + short SHA) — its parsed `version` is only ever the raw
      // non-semver tag, which says nothing about which build it is.
      const devBuildId = v.prerelease ? extractDevBuildId(v.asset_name) : null;
      let label = devBuildId ?? `v${v.version}`;

      // currentBuildId === null gates the stable branch: dev builds are
      // cut before package.json's version bumps, so a genuinely-installed
      // dev build's `currentVersion` is identical to the officially
      // tagged stable release sharing that frozen base version. Without
      // this guard both rows would show "(installed)" at once whenever a
      // dev build happens to share its base version with a real release.
      const isInstalled = v.prerelease
        ? devBuildId !== null && devBuildId === currentBuildId
        : currentBuildId === null && v.version === currentVersion;

      if (isInstalled) {
        label += ` (${t("updater.installedLabel", {
          defaultValue: "installed",
        })})`;
      } else if (updateData?.latest?.version === v.version) {
        label += ` (${t("updater.latestLabel", { defaultValue: "latest" })})`;
      }

      if (v.prerelease) {
        label += " [DEV]";
      }

      return {
        data: v.tag,
        label,
      };
    });
  }, [versionsData, currentVersion, currentBuildId, updateData, t]);

  const handleVersionSelect = (opt: { data: string }) => {
    const tag = String(opt.data);
    setPersistedSelectedTag(tag); // survive the dismount that follows selection
    setSelectedTag(tag);
  };

  const handleCheckUpdate = async () => {
    setChecking(true);
    try {
      // Force GitHub to be re-queried (bypasses the 1-hour cache), then
      // pull the now-warm result into the displayed query state — this
      // second pair only hits the in-process cache, no extra GitHub call.
      await Promise.all([forceCheckMut.mutate(), forceVersionsMut.mutate()]);
      await Promise.all([checkUpdate(), refetchVersions()]);
      toast.success(
        t("updater.checkCompleteTitle", {
          defaultValue: "Update Check Complete",
        }),
        t("updater.checkCompleteMessage", {
          defaultValue: "Successfully fetched latest version info.",
        }),
      );
    } catch (e) {
      const message = e instanceof Error ? e.message : undefined;
      toast.error(
        t("updater.checkFailedTitle", { defaultValue: "Check Failed" }),
        message ?? t("errors.unknown"),
      );
    } finally {
      setChecking(false);
    }
  };

  const handleShowReleaseNotes = () => {
    if (!selectedRelease) return;
    showModal(
      <ReleaseNotesModal
        version={selectedVersion}
        body={selectedRelease.body}
      />,
    );
  };

  const handleInstall = async () => {
    if (!selectedRelease) return;

    const backend = getDeckyBackend();
    if (!backend) {
      toast.error(
        t("updater.installFailedTitle", { defaultValue: "Install Failed" }),
        t("updater.noBackend", {
          defaultValue: "Decky backend is unavailable.",
        }),
      );
      return;
    }

    setInstalling(true);
    try {
      // Prerelease/dev tags are mutable — their single GitHub asset gets
      // deleted and re-uploaded under a new name every time a new dev
      // build is cut, so a URL sitting in React state can already be
      // dead. Force a fresh fetch and re-resolve by tag (the stable
      // identifier) before ever handing a URL to Decky's installer.
      let release = selectedRelease;
      if (release.prerelease) {
        const fresh = await forceVersionsMut.mutate();
        // `mutate()` resolves `null` ONLY when the refresh call itself
        // failed (network hiccup, GitHub rate limit, backend error) —
        // a successful call always resolves an array, even an empty
        // one. Treating that failure the same as "genuinely gone" is
        // misleading: it tells the user their release vanished when in
        // fact we simply couldn't check, which is confusing when GitHub
        // shows the release is right there.
        if (fresh === null) {
          toast.error(
            t("updater.installFailedTitle", { defaultValue: "Install Failed" }),
            t("updater.refreshFailedMessage", {
              defaultValue:
                "Could not verify the latest release info. Check your connection and try again.",
            }),
          );
          setInstalling(false);
          return;
        }
        const match = fresh.find((v) => v.tag === release.tag);
        if (!match) {
          toast.error(
            t("updater.installFailedTitle", { defaultValue: "Install Failed" }),
            t("updater.releaseGoneMessage", {
              defaultValue:
                "This release is no longer available. Please Check for Updates and select again.",
            }),
          );
          setInstalling(false);
          return;
        }
        release = match;
      }

      const { installType, displayVersion } = resolveInstallAction(
        release,
        currentVersion,
        currentBuildId,
      );
      const typeLabel =
        installType === INSTALL_TYPE_REINSTALL
          ? t("updater.typeReinstall", { defaultValue: "Reinstalling" })
          : installType === INSTALL_TYPE_DOWNGRADE
          ? t("updater.typeDowngrade", { defaultValue: "Downgrading to" })
          : t("updater.typeUpdate", { defaultValue: "Updating to" });

      logEvent(
        "triggered",
        `${typeLabel} v${displayVersion} (type=${installType}) url=${release.asset_url}`,
      );
      toast.info(
        t("updater.installingTitle", { defaultValue: "Installing Plugin" }),
        `${typeLabel} v${displayVersion}...`,
      );

      // Hand off to Decky Loader's installer via the GLOBAL ws router.
      // (`call` from @decky/api is plugin-scoped and cannot reach utilities/*.)
      // This only registers the request and pops Decky's native confirm modal
      // (which has its own progress bar); it returns immediately. Decky's modal
      // calls confirm_plugin_install on OK; our listeners mirror the progress.
      // We deliberately stay open so that progress is visible underneath it.
      await backend.call(
        "utilities/install_plugin",
        release.asset_url,
        "Unifideck",
        displayVersion,
        release.sha256 || "",
        installType,
      );
    } catch (e) {
      const message = e instanceof Error ? e.message : undefined;
      logEvent("error", message ?? String(e));
      toast.error(
        t("updater.installFailedTitle", { defaultValue: "Install Failed" }),
        message ?? t("errors.unknown"),
      );
    } finally {
      // The call returns before the install runs; never leave the modal locked.
      // Decky's confirm modal + the loader progress events own the rest.
      setInstalling(false);
    }
  };

  const isLoading = checkingOnMount || loadingVersions;
  const busy = installing || downloadActive || checking;

  // Same resolveInstallAction used by handleInstall — previously this
  // button independently re-derived update/downgrade/reinstall via its
  // own compareVersions() call, which could disagree with (and, for
  // prerelease rows, was as wrong as) the logic actually driving the
  // install request itself.
  const installButtonLabel = useMemo(() => {
    if (downloadActive) {
      return t("updater.installingButton", { defaultValue: "Installing..." });
    }
    if (!selectedRelease) return "";
    const { installType, displayVersion } = resolveInstallAction(
      selectedRelease,
      currentVersion,
      currentBuildId,
    );
    if (installType === INSTALL_TYPE_REINSTALL) {
      return t("updater.reinstallButton", {
        version: displayVersion,
        defaultValue: `Reinstall v${displayVersion}`,
      });
    }
    if (installType === INSTALL_TYPE_DOWNGRADE) {
      return t("updater.downgradeButton", {
        version: displayVersion,
        defaultValue: `Downgrade to v${displayVersion}`,
      });
    }
    return t("updater.updateButton", {
      version: displayVersion,
      defaultValue: `Update to v${displayVersion}`,
    });
  }, [downloadActive, selectedRelease, currentVersion, currentBuildId, t]);

  const currentLine = currentBuildId
    ? `${t("updater.currentTitle", {
        defaultValue: "Current",
      })} - v${currentVersion} (${currentBuildId})`
    : `${t("updater.currentTitle", {
        defaultValue: "Current",
      })} - v${currentVersion}`;

  return (
    <ConfirmModal
      strTitle={t("updater.manageTitle", { defaultValue: "Plugin Update" })}
      bAlertDialog
      strOKButtonText={t("common.close", { defaultValue: "Close" })}
      onOK={closeModal}
      onCancel={closeModal}
      bHideCloseIcon={false}
    >
      {isLoading ? (
        <div style={{ textAlign: "center", padding: "20px", opacity: 0.6 }}>
          {t("common.loading", { defaultValue: "Loading..." })}
        </div>
      ) : (
        <>
          <div
            style={{
              marginBottom: 12,
              fontSize: 13,
              opacity: 0.7,
            }}
          >
            {currentLine}
          </div>

          {versionOptions.length > 0 && (
            <div
              style={{
                padding: 12,
                background: "rgba(0, 0, 0, 0.2)",
                borderRadius: 8,
              }}
            >
              <Dropdown
                rgOptions={versionOptions}
                selectedOption={selectedTag}
                onChange={handleVersionSelect}
                disabled={downloadActive || checking}
              />
            </div>
          )}

          {downloadActive && (
            <div style={{ marginTop: 12 }}>
              <ProgressBarWithInfo
                layout="inline"
                bottomSeparator="none"
                nProgress={downloadPercent}
                sOperationText={downloadStatus}
              />
            </div>
          )}

          <Focusable style={{ marginTop: 12 }}>
            <DialogButton
              disabled={busy || !selectedRelease}
              onClick={() => void handleInstall()}
              style={{
                width: "100%",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                gap: 8,
              }}
            >
              <FaDownload />
              {installButtonLabel}
            </DialogButton>
          </Focusable>

          <Focusable style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <DialogButton
              disabled={busy || !selectedRelease}
              onClick={handleShowReleaseNotes}
              style={{ flex: 1 }}
            >
              {t("updater.releaseNotesButton", {
                defaultValue: "Release Notes",
              })}
            </DialogButton>
            <DialogButton
              disabled={busy}
              onClick={() => void handleCheckUpdate()}
              style={{ flex: 1 }}
            >
              {checking
                ? t("updater.checkingButton", { defaultValue: "Checking..." })
                : t("updater.checkButton", {
                    defaultValue: "Check for Updates",
                  })}
            </DialogButton>
          </Focusable>
        </>
      )}
    </ConfirmModal>
  );
};
