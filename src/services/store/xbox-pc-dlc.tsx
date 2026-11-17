/**
 * Xbox PC DLC flows around `DlcSelectModal`.
 *
 *  - {@link pickDlcForInstall}: the install flow asks before the storage
 *    picker when the game has large DLC. The choice is saved on the backend
 *    (`set_xbox_pc_dlc`), which applies it once the game installs.
 *  - {@link manageXboxPcDlc}: the game page's DLC button.
 *  - {@link DLC_VERB}: a start that bypassed the Update button refuses in
 *    the launcher with `dlc_pending`; the launcher-toast poll calls
 *    {@link installXboxPcDlc} at once, so the DLC downloads with no extra
 *    tap, as a normal queue row.
 *
 * Every DLC download is a download-queue job (an Update), never a launcher
 * step. A refusal toasts why (rule 8).
 */
import { showModal } from "@decky/ui";
import { rpcRoutes } from "../../api/rpc-routes";
import {
  DlcSelectModal,
  type DlcModalMode,
} from "../../components/modals/DlcSelectModal";
import { fetchXboxPcDlc, setXboxPcDlc } from "../../hooks/useXboxPcDlc";
import { downloadErrorToast } from "../../lib/download-errors";
import { callXboxPc } from "../../stores/xbox-pc-store";
import { headerText } from "../toast-text";
import { showToast } from "../show-toast";
import type { XboxPcDlcList } from "../../types/api";

/** The toast action verb the launcher sends with `dlc_pending`. */
export const DLC_VERB = "install-xbox-pc-dlc";

function failed(error: string | null | undefined): void {
  showToast({
    title: headerText("toasts.header.xboxPc"),
    body: downloadErrorToast(error ?? undefined),
    duration: 7000,
  });
}

function openDlcModal(
  mode: DlcModalMode,
  title: string,
  list: XboxPcDlcList,
  baseBytes = 0,
): Promise<string[] | null> {
  return new Promise((resolve) => {
    let confirmed = false;
    const handle = showModal(
      <DlcSelectModal
        mode={mode}
        gameTitle={title}
        dlc={list.dlc}
        installed={list.installed}
        baseBytes={baseBytes}
        onConfirm={(selected) => {
          confirmed = true;
          resolve(selected);
        }}
        closeModal={() => {
          handle?.Close();
          if (!confirmed) resolve(null);
        }}
      />,
    );
  });
}

/**
 * The install picker. Resolves the bytes the chosen DLC adds (for the
 * storage picker), or null when the user cancelled the install. It opens
 * with what the game page's DLC list saved; small DLC keeps that choice
 * too. A game with no large DLC skips the picker.
 */
export async function pickDlcForInstall(
  gameId: string,
  title: string,
  list: XboxPcDlcList,
  baseBytes = 0,
): Promise<number | null> {
  const small = list.dlc.filter((d) => !d.large && d.chosen);
  const smallBytes = small.reduce((s, d) => s + (d.size_bytes ?? 0), 0);
  if (!list.applicable || !list.dlc.some((d) => d.large)) return smallBytes;
  const picked = await openDlcModal("install", title, list, baseBytes);
  if (picked == null) return null;
  const r = await setXboxPcDlc(gameId, [
    ...small.map((d) => d.product_id),
    ...picked,
  ]);
  if (!r.success) failed(r.error);
  const largeBytes = list.dlc
    .filter((d) => picked.includes(d.product_id))
    .reduce((s, d) => s + (d.size_bytes ?? 0), 0);
  return smallBytes + largeBytes;
}

/** The game page's DLC button. Before install the choice is saved for the
 *  install picker. Resolves true when the choice was saved. */
export async function manageXboxPcDlc(
  gameId: string,
  title: string,
  list: XboxPcDlcList,
): Promise<boolean> {
  const picked = await openDlcModal("manage", title, list);
  if (picked == null) return false;
  const r = await setXboxPcDlc(gameId, picked);
  if (!r.success) failed(r.error);
  return r.success;
}

/** Queue the pending DLC of `gameId` (the launcher toast's action). */
export async function installXboxPcDlc(gameId: string): Promise<boolean> {
  const r = await callXboxPc<unknown>(rpcRoutes.installXboxPcDlc, gameId);
  if (r.ok) return true;
  failed(r.error);
  return false;
}

interface CompletedItem {
  store?: string;
  game_id?: string;
  is_update?: boolean;
}

/**
 * After an Xbox PC game installs, download its chosen DLC as its own
 * Update job (a queue row with progress). The install job ends with the
 * base game, so cancelling the DLC leaves the game installed and its Play
 * shows Update. A game with no pending DLC is answered
 * `xbox_dlc_nothing_pending`, which is not worth a toast.
 */
export async function continueDlcAfterInstall(payload: unknown): Promise<void> {
  const item = (payload as { item?: CompletedItem } | null)?.item;
  if (!item || item.store !== "microsoft" || item.is_update || !item.game_id) {
    return;
  }
  const r = await callXboxPc<unknown>(rpcRoutes.installXboxPcDlc, item.game_id);
  if (!r.ok && r.error !== "xbox_dlc_nothing_pending") failed(r.error);
}

/** How long Install waits for the DLC list before going on without it. */
export const DLC_LIST_WAIT_MS = 15000;

/**
 * The DLC list for the install picker, or null when it did not come in
 * time. Install then goes on without the picker (small DLC still installs;
 * large DLC can be ticked on the game page later) and says so, rather than
 * leaving the button doing nothing.
 */
export async function dlcListForInstall(
  game: Parameters<typeof fetchXboxPcDlc>[0],
): Promise<XboxPcDlcList | null> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const late = new Promise<null>((resolve) => {
    timer = setTimeout(() => resolve(null), DLC_LIST_WAIT_MS);
  });
  try {
    const list = await Promise.race([fetchXboxPcDlc(game), late]);
    if (list == null) failed("xbox_dlc_unknown");
    return list;
  } finally {
    clearTimeout(timer);
  }
}
