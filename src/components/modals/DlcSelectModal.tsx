/**
 * DlcSelectModal — which DLC of an Xbox PC game to install.
 *
 * Two modes, one list (`useXboxPcDlc` / `get_xbox_pc_dlc`):
 *
 *  - `install`: shown by the install flow before the storage picker, only
 *    when the game has large DLC (over 1 GiB). It lists the large DLC,
 *    ticked as the DLC page left them (all of them when nothing was
 *    chosen); small DLC is only counted. Confirm resolves the ticked
 *    large ids.
 *  - `manage`: the game page's DLC button. Every owned DLC with its state.
 *    Before install, the ticks are saved and the install picker opens
 *    with them. On an installed game, ticking a DLC makes Play show Update
 *    (it downloads when the user presses it) and unticking removes it.
 *
 * Pure presentational: the caller saves the choice.
 */
import { FC, useMemo, useState } from "react";
import { ConfirmModal, Focusable, ToggleField } from "@decky/ui";
import { useTranslation } from "react-i18next";
import type { XboxPcDlc } from "../../types/api";

export type DlcModalMode = "install" | "manage";

interface Props {
  mode: DlcModalMode;
  gameTitle: string;
  dlc: XboxPcDlc[];
  /** False before the game is installed: the choice is then saved for
   *  the install. */
  installed: boolean;
  /** Install mode: the game's own download size, for the total. 0 = unknown. */
  baseBytes?: number;
  onConfirm: (selected: string[]) => void;
  closeModal?: () => void;
}

/** Decimal GB, as the storage picker that follows shows sizes. */
function formatSize(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  if (bytes <= 0) return "0 MB";
  return `${Math.max(1, Math.round(bytes / 1e6))} MB`;
}

function sizeLabel(dlc: XboxPcDlc, unknown: string): string {
  return dlc.size_bytes == null ? unknown : formatSize(dlc.size_bytes);
}

export const DlcSelectModal: FC<Props> = ({
  mode,
  gameTitle,
  dlc,
  installed,
  baseBytes = 0,
  onConfirm,
  closeModal,
}) => {
  const { t } = useTranslation();
  const shown = useMemo(
    () => (mode === "install" ? dlc.filter((d) => d.large) : dlc),
    [mode, dlc],
  );
  const smallCount =
    mode === "install" ? dlc.filter((d) => !d.large && d.chosen).length : 0;
  const [ticked, setTicked] = useState<Set<string>>(
    () => new Set(shown.filter((d) => d.chosen).map((d) => d.product_id)),
  );
  const total = shown
    .filter((d) => ticked.has(d.product_id))
    .reduce((sum, d) => sum + (d.size_bytes ?? 0), 0);
  // Install mode: the whole download, the game plus every add-on that
  // comes with it (the small ones the DLC page left ticked, and the
  // large ones ticked here).
  const smallBytes =
    mode === "install"
      ? dlc
          .filter((d) => !d.large && d.chosen)
          .reduce((sum, d) => sum + (d.size_bytes ?? 0), 0)
      : 0;
  const addonBytes = total + smallBytes;

  const toggle = (pid: string, on: boolean) =>
    setTicked((prev) => {
      const next = new Set(prev);
      if (on) next.add(pid);
      else next.delete(pid);
      return next;
    });

  // A DLC that is not chosen needs no state: its switch already says so.
  const stateText = (d: XboxPcDlc): string =>
    d.state === "declined" ? "" : t(`dlcModal.state.${d.state}`);

  return (
    <ConfirmModal
      strTitle={t("dlcModal.title")}
      strDescription={t(
        mode === "manage" && !installed
          ? "dlcModal.chooseAtInstall"
          : `dlcModal.description.${mode}`,
        { title: gameTitle },
      )}
      strOKButtonText={t(
        mode === "install" ? "dlcModal.install" : "dlcModal.save",
      )}
      strCancelButtonText={t("common.cancel")}
      onOK={() => {
        onConfirm([...ticked]);
        closeModal?.();
      }}
      onCancel={closeModal}
      bHideCloseIcon={false}
    >
      <Focusable style={{ maxHeight: "50vh", overflowY: "auto" }}>
        {shown.map((d) => (
          <ToggleField
            key={d.product_id}
            label={d.title}
            description={[sizeLabel(d, t("dlcModal.sizeUnknown")), stateText(d)]
              .filter(Boolean)
              .join(" · ")}
            checked={ticked.has(d.product_id)}
            onChange={(on: boolean) => toggle(d.product_id, on)}
          />
        ))}
      </Focusable>
      <div style={{ marginTop: 12, fontSize: 13, opacity: 0.8 }}>
        {mode === "install" ? (
          <>
            <div>
              {t("dlcModal.totalDownload", {
                size: formatSize(baseBytes + addonBytes),
              })}
            </div>
            <div>
              {baseBytes > 0
                ? t("dlcModal.breakdown", {
                    game: formatSize(baseBytes),
                    addons: formatSize(addonBytes),
                  })
                : t("dlcModal.addonsOnly", { addons: formatSize(addonBytes) })}
            </div>
          </>
        ) : (
          t("dlcModal.total", { size: formatSize(total) })
        )}
        {smallCount > 0 && (
          <div>{t("dlcModal.smallIncluded", { count: smallCount })}</div>
        )}
      </div>
    </ConfirmModal>
  );
};
