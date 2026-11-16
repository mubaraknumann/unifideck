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
  cloudSaveViewLine,
  useCloudSaveView,
} from "../../hooks/useCloudSaveView";
import type { CloudLine } from "../../lib/cloud-save-line";
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

/**
 * One line for every store. `open` makes it focusable and selectable (a store
 * we sync); without it the line only reports (Xbox PC, synced by its runtime).
 */
const CloudStatusLine: FC<{
  line: CloudLine;
  open?: () => void;
  disabled?: boolean;
}> = ({ line, open, disabled }) => {
  const { t } = useTranslation();
  const [el, setEl] = useState<HTMLDivElement | null>(null);
  const c = getNativeClasses().playBar;
  const labelSel = selectorOf(c.CloudStatusLabel);
  const iconSel = selectorOf(c.CloudStatusIcon);
  useRestingColours(el, labelSel, iconSel);
  const text = line.token ? steamText(line.token, t(line.key)) : t(line.key);
  const body = (
    <CloudLineBody
      Icon={line.busy ? FaSyncAlt : FaCloud}
      spin={line.busy}
      label={t("play.cloudSave.label")}
      text={text}
    />
  );
  if (!open) {
    return (
      <div
        className={cx(c.CloudStatusRow, line.problem && c.CloudStatusSyncFail)}
      >
        {body}
      </div>
    );
  }
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
        line.problem && c.CloudStatusSyncFail,
      )}
      onActivate={disabled ? undefined : open}
      onClick={disabled ? undefined : open}
      noFocusRing={false}
    >
      <style>{pinned}</style>
      {body}
    </Focusable>
  );
};

/** A store we sync: Epic, GOG, EA. */
const SyncedRow: FC<{ game: Game }> = ({ game }) => {
  const view = useCloudSaveView(game.store, game.id, game.title);
  const line = view.visible ? cloudSaveViewLine(view) : null;
  if (!line) return null;
  return (
    <CloudStatusLine line={line} open={view.open} disabled={view.disabled} />
  );
};

/** Xbox PC: its runtime syncs inside the game, so the line only reports. */
const RuntimeRow: FC<{ game: Game }> = ({ game }) => {
  const status = useXboxSaveStatus(game.id);
  const line = status?.applicable ? xboxSaveLine(status.state) : null;
  return line ? <CloudStatusLine line={line} /> : null;
};

export const NativeCloudStatusRow: FC<{ appId: number }> = ({ appId }) => {
  const { data: game } = useGameInfo(appId);
  if (!game) return null;
  return usesXboxSaveLine(game) ? (
    <RuntimeRow game={game} />
  ) : (
    <SyncedRow game={game} />
  );
};
