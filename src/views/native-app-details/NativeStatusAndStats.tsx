/**
 * NativeStatusAndStats: the middle of the play bar for our games.
 *
 * Replaces Steam's `StatusAndStats` element (which reads Steam's own
 * playtime and download store) with the same markup and classes, fed by
 * our data:
 *
 *   [status line]  Installed size · Last played · Played · Cloud saves
 *
 * The status line is Steam's `DetailsSection` (label + detail + progress
 * bar), used for a download in progress and for an Xbox PC title that
 * cannot install. Every number comes from `usePlayStats`, the same hook
 * the legacy row uses.
 */
import { FC, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { DownloadProvider } from "../../contexts/DownloadContext";
import { useGameInfo } from "../../hooks/useGameInfo";
import {
  usePlaySection,
  type PlaySectionState,
} from "../../hooks/usePlaySection";
import { usePlayStats } from "../../hooks/usePlayStats";
import {
  AppAssetType,
  cx,
  getAppImage,
  getNativeClasses,
} from "../../lib/steam-bridge/native-app-details";
import { pcUnsupportedKey } from "../../lib/xbox-pc-errors";
import { describeDownload } from "../../components/downloads/download-progress-text";
import {
  CircuitBadge,
  cloudSaveValueKey,
  formatBytes,
  formatLastPlayed,
  formatLastPlayedISO,
  formatPlaytime,
} from "../../components/play/PlayMeta";
import { pcUnsupportedButtonKey } from "../../components/play/PcUnsupportedButtons";
import type { DownloadItem } from "../../types/downloads";

interface Props {
  appId: number;
  overview: object;
}

/** One stat, in Steam's `GameStat` markup. */
const Stat: FC<{ label: string; value: string }> = ({ label, value }) => {
  const c = getNativeClasses().playBar;
  return (
    <div className={cx(c.GameStat, c.LastPlayed)}>
      <div className={cx(c.GameStatRight, c.LastPlayedRight)}>
        <div className={cx(c.PlayBarLabel, c.LastPlayedLabel)}>{label}</div>
        <div className={cx(c.PlayBarDetailLabel, c.LastPlayedInfo)}>
          {value}
        </div>
      </div>
    </div>
  );
};

/** Steam's status line: a label, a detail line and an optional bar. */
const StatusLine: FC<{
  label: string;
  detail: ReactNode;
  downloading?: boolean;
  bar?: ReactNode;
}> = ({ label, detail, downloading, bar }) => {
  const c = getNativeClasses().playBar;
  return (
    <div className={cx(c.DetailsSection, downloading && c.Downloading)}>
      <div className={cx(c.PlayBarLabel, c.DetailsSectionStatus)}>{label}</div>
      <div className={cx(c.PlayBarDetailLabel, c.DetailsSectionExtra)}>
        {detail}
        {bar}
      </div>
    </div>
  );
};

/** A download's words. Its bar is drawn under the action button, where
 *  Steam draws its own (`NativeDownloadBar`). */
const DownloadStatus: FC<{ download: DownloadItem }> = ({ download }) => {
  const { t } = useTranslation();
  const d = describeDownload(download, t);
  const detail = d.indeterminate
    ? d.detail
    : [d.bytes, d.rate].filter(Boolean).join(" · ");
  return <StatusLine label={d.label} detail={detail} downloading />;
};

/** The status line for this state, or null. */
function statusFor(
  state: PlaySectionState,
  t: (k: string) => string,
): ReactNode {
  if (state.kind === "downloading") {
    return <DownloadStatus download={state.download} />;
  }
  if (state.kind === "pc-unsupported") {
    return (
      <StatusLine
        label={t(pcUnsupportedButtonKey(state.reason))}
        detail={t(pcUnsupportedKey(state.reason))}
      />
    );
  }
  if (state.kind === "browser" && state.notice) {
    return (
      <StatusLine
        label={t("play.playOnCloud")}
        detail={t(pcUnsupportedKey(state.notice))}
      />
    );
  }
  return null;
}

/** Steam's icon + name pair, shown when the play bar pins to the top. */
const IconAndName: FC<{ overview: object; name: string }> = ({
  overview,
  name,
}) => {
  const c = getNativeClasses().playBar;
  const AppImage = getAppImage();
  return (
    <div className={c.PlayBarIconAndGame}>
      {AppImage && (
        <AppImage
          app={overview}
          eAssetType={AppAssetType.Icon}
          className={c.PlayBarGameIcon}
        />
      )}
      <span className={c.PlayBarGameName}>{name}</span>
    </div>
  );
};

const StatusAndStats: FC<Props> = ({ appId, overview }) => {
  const { t } = useTranslation();
  const c = getNativeClasses().playBar;
  const state = usePlaySection(appId);
  const { data: game } = useGameInfo(appId);
  const installed = Boolean(game?.is_installed);
  const isBrowser = state.kind === "browser";
  const stats = usePlayStats({
    appId,
    store: isBrowser ? null : game?.store,
    gameId: isBrowser ? null : game?.id,
    sizeBytes: game?.size_bytes,
    installed,
    withPlaytime: true,
  });
  const lastPlayed =
    formatLastPlayedISO(stats.lastPlayedIso) ??
    (stats.steamLastPlayed ? formatLastPlayed(stats.steamLastPlayed) : null);

  return (
    <div className={c.StatusAndStats}>
      {statusFor(state, t)}
      <div className={c.StatusNameContainer}>
        <div className={c.GameStatsSection}>
          {!isBrowser && (
            <Stat
              label={
                installed
                  ? t("playMeta.installedSize")
                  : t("playMeta.spaceRequired")
              }
              value={formatBytes(stats.sizeBytes)}
            />
          )}
          <Stat
            label={t("playMeta.lastPlayed")}
            value={lastPlayed ?? t("playMeta.neverPlayed")}
          />
          {stats.playedSecs != null && (
            <Stat
              label={t("playMeta.played")}
              value={formatPlaytime(
                stats.playedSecs,
                t("playMeta.hoursShort"),
                t("playMeta.minutesShort"),
              )}
            />
          )}
          {stats.cloudSaves !== "hidden" && (
            <Stat
              label={t("playMeta.cloudSaves")}
              value={t(cloudSaveValueKey(stats.cloudSaves))}
            />
          )}
          {stats.circuit.open && (
            <CircuitBadge
              failureCount={stats.circuit.failureCount}
              onReset={() => void stats.circuit.reset()}
              onForceLaunch={() => void stats.circuit.forceLaunch()}
            />
          )}
        </div>
        <IconAndName overview={overview} name={game?.title ?? ""} />
      </div>
    </div>
  );
};

export const NativeStatusAndStats: FC<Props> = (props) => (
  <DownloadProvider>
    <StatusAndStats {...props} />
  </DownloadProvider>
);
