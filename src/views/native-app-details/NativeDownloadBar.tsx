/**
 * NativeDownloadBar: the download progress bar under the action button.
 *
 * On a Steam game Steam draws a download's bar in an `OnlyDownloadBar`
 * directly under the Play / Pause button: an absolutely positioned,
 * full-width, 4 px bar (`DetailsProgressContainer` + `DownloadProgressBar`).
 * Its status area beside the button carries only the words. This is the
 * same bar for our download, rendered by the action-button host inside the
 * button's own column, so it lands exactly where Steam's does.
 *
 * Measured live 2026-11-02: drawn inside the status area instead, the
 * `DownloadProgressBar` rules pinned the bar to that area's bottom edge as
 * a full-width hairline, which did not show in Gaming Mode.
 *
 * Phases without a percentage (a client-driven `manual` install,
 * extracting, verifying) show our sliding bar instead of a fill.
 */
import { FC } from "react";
import { useTranslation } from "react-i18next";
import { usePlaySection } from "../../hooks/usePlaySection";
import {
  cx,
  getNativeClasses,
} from "../../lib/steam-bridge/native-app-details";
import { describeDownload } from "../../components/downloads/download-progress-text";
import { PLAY_FOCUS_CSS } from "../../components/play/play.css";

export const NativeDownloadBar: FC<{ appId: number }> = ({ appId }) => {
  const { t } = useTranslation();
  const state = usePlaySection(appId);
  if (state.kind !== "downloading") return null;
  const c = getNativeClasses().playBar;
  const d = describeDownload(state.download, t);
  const paused = state.download.status !== "running";
  return (
    <div className={c.OnlyDownloadBar}>
      <div
        role="progressbar"
        aria-valuenow={d.pct}
        className={cx(
          c.DetailsProgressContainer,
          c.DownloadProgressBar,
          paused && c.DownloadPaused,
        )}
        // The sliding bar moves inside the track; keep it clipped to it.
        style={d.indeterminate ? { overflow: "hidden" } : undefined}
      >
        {d.indeterminate ? (
          <>
            {/* The slide animation (play.css.ts), rendered here so it lands
                in this page's document: `injectPlayFocusStyles` writes to
                the shared JS context's document, which this page never
                reads. */}
            <style>{PLAY_FOCUS_CSS}</style>
            <div className="unifideck-progress-indeterminate" />
          </>
        ) : (
          <div
            className={c.DetailsProgressBar}
            style={{ width: `${d.pct}%` }}
          />
        )}
      </div>
    </div>
  );
};
