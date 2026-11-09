/**
 * NativeCloudStatusRow: "CLOUD SAVES: UP TO DATE" under the play bar.
 *
 * Steam shows "STEAM CLOUD: UP TO DATE" under the play bar of a Steam
 * game with Steam Cloud. This is the same row (Steam's `CloudStatusRow`
 * markup and status words) for our store's cloud saves, from our status
 * (`useCloudSaveView`). It is focusable, and selecting it opens our
 * Download / Upload dialog. Hidden for a store or game without cloud saves.
 *
 * An installed Xbox PC game gets a plain line with no action
 * (`useXboxSaveStatus`). Its runtime syncs inside the game, and Unifideck never
 * pushes a save from outside it, so there is nothing to select.
 */
import { FC, ReactNode, useEffect, useState } from "react";
import { Focusable } from "@decky/ui";
import { useTranslation } from "react-i18next";
import type { IconType } from "react-icons";
import { FaCloud, FaSyncAlt } from "react-icons/fa";
import { useGameInfo } from "../../hooks/useGameInfo";
import {
  useCloudSaveView,
  type CloudSaveState,
} from "../../hooks/useCloudSaveView";
import {
  cx,
  getNativeClasses,
  steamText,
} from "../../lib/steam-bridge/native-app-details";
import {
  usesXboxSaveLine,
  useXboxSaveStatus,
  xboxSaveLine,
} from "../../hooks/useXboxSaveStatus";
import type { Game } from "../../types/api";

/** Steam's own status word for each of our states, and our fallback key. */
const STATUS: Record<
  Exclude<CloudSaveState, "no-support">,
  [token: string, fallbackKey: string]
> = {
  checking: [
    "#AppDetails_CloudStatus_Checking",
    "play.cloudSave.statusSyncing",
  ],
  syncing: ["#AppDetails_CloudStatus_Checking", "play.cloudSave.statusSyncing"],
  unresolved: [
    "#AppDetails_CloudStatus_SyncFailed",
    "play.cloudSave.statusUnresolved",
  ],
  "out-of-sync": [
    "#AppDetails_CloudStatus_OutOfSync",
    "play.cloudSave.statusCloudAvailable",
  ],
  "in-sync": [
    "#AppDetails_CloudStatus_Synchronized",
    "play.cloudSave.statusInSync",
  ],
  empty: [
    "#AppDetails_CloudStatus_Synchronized",
    "play.cloudSave.statusInSync",
  ],
  // Our own words: Steam has none for this.
  "check-connection": ["", "play.cloudSave.checkConnection"],
};

/** First class of a CSS-module name, for a selector. */
function selectorOf(name: string | undefined): string {
  const first = (name ?? "").split(" ")[0];
  return first ? `.${first}` : "";
}

const ROW_MARKER = "unifideck-cloud-row";

/**
 * Keep the line's colours when it has focus. Steam styles a focused
 * `CloudStatusRow` for its own problem state (a white pill with black
 * text, `.gpfocus .CloudStatusLabel`), which on our transparent line left
 * black text on a dark page. The resting colours are read once from the
 * rendered line (whatever Steam or a theme computed) into CSS variables,
 * and the focused line keeps them. Steam's focus ring marks the focus.
 */
function useRestingColours(
  el: HTMLElement | null,
  labelSel: string,
  iconSel: string,
) {
  useEffect(() => {
    if (!el) return;
    const view = el.ownerDocument.defaultView;
    const label = labelSel ? el.querySelector(labelSel) : null;
    const icon = iconSel ? el.querySelector(iconSel) : null;
    if (label && view) {
      el.style.setProperty(
        "--ud-cloud-label",
        view.getComputedStyle(label).color,
      );
    }
    if (icon && view) {
      el.style.setProperty(
        "--ud-cloud-icon",
        view.getComputedStyle(icon).color,
      );
    }
  }, [el, labelSel, iconSel]);
}

/** The icon and "CLOUD SAVES: <status>" text, in Steam's markup. */
const CloudLineBody: FC<{
  Icon: IconType;
  spin: boolean;
  label: string;
  text: ReactNode;
}> = ({ Icon, spin, label, text }) => {
  const c = getNativeClasses().playBar;
  return (
    <>
      <span className={c.CloudStatusIcon}>
        <Icon className={cx(c.CloudIconSVG, spin && "unifideck-cloud-spin")} />
      </span>
      <span className={c.CloudStatusLabel}>
        {label}: <span>{text}</span>
      </span>
    </>
  );
};

const Row: FC<{ game: Game }> = ({ game }) => {
  const { t } = useTranslation();
  const view = useCloudSaveView(game.store, game.id, game.title);
  const [el, setEl] = useState<HTMLDivElement | null>(null);
  const c = getNativeClasses().playBar;
  const labelSel = selectorOf(c.CloudStatusLabel);
  const iconSel = selectorOf(c.CloudStatusIcon);
  useRestingColours(el, labelSel, iconSel);
  if (!view.visible || view.state === "no-support") return null;
  const [token, fallbackKey] = STATUS[view.state];
  const failed = view.state === "unresolved" || view.state === "out-of-sync";
  const Icon = view.state === "syncing" ? FaSyncAlt : FaCloud;
  const pinned = [
    labelSel &&
      `.${ROW_MARKER}.gpfocus ${labelSel}{color:var(--ud-cloud-label)!important}`,
    iconSel &&
      `.${ROW_MARKER}.gpfocus ${iconSel}{color:var(--ud-cloud-icon)!important}`,
    `.${ROW_MARKER}.gpfocus{background:transparent!important}`,
  ]
    .filter(Boolean)
    .join("");
  return (
    <Focusable
      ref={setEl}
      className={cx(
        ROW_MARKER,
        c.CloudStatusRow,
        failed && c.CloudStatusSyncFail,
      )}
      onActivate={view.disabled ? undefined : view.open}
      onClick={view.disabled ? undefined : view.open}
      noFocusRing={false}
    >
      <style>{pinned}</style>
      <CloudLineBody
        Icon={Icon}
        spin={view.state === "syncing"}
        label={t("play.cloudSave.label")}
        text={token ? steamText(token, t(fallbackKey)) : t(fallbackKey)}
      />
    </Focusable>
  );
};

const XboxRow: FC<{ game: Game }> = ({ game }) => {
  const { t } = useTranslation();
  const status = useXboxSaveStatus(game.id);
  const line = status?.applicable ? xboxSaveLine(status.state) : null;
  if (!line) return null;
  const c = getNativeClasses().playBar;
  const text = line.token ? steamText(line.token, t(line.key)) : t(line.key);
  return (
    <div className={c.CloudStatusRow}>
      <CloudLineBody
        Icon={FaCloud}
        spin={false}
        label={t("play.cloudSave.label")}
        text={text}
      />
    </div>
  );
};

export const NativeCloudStatusRow: FC<{ appId: number }> = ({ appId }) => {
  const { data: game } = useGameInfo(appId);
  if (!game) return null;
  return usesXboxSaveLine(game) ? <XboxRow game={game} /> : <Row game={game} />;
};
