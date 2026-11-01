/**
 * ChangeExecutableModal — pick the executable a game launches.
 *
 * Opened from the "Change executable…" item injected into the native game
 * context menu (see {@link file://./../../lib/steam-bridge/app-context-menu-patch.ts}).
 * Lets the user point a shortcut at a different binary — skip a launcher
 * (Fallout New Vegas → ``FalloutNV.exe``), fix a wrong auto-detected path, or
 * run a config tool — WITHOUT hand-editing the DO-NOT-EDIT ``games.map`` (which
 * corrupts ``work_dir`` and breaks saves).
 *
 * The choice is fully decoupled from ``work_dir``: the backend only ever writes
 * the executable, never the working directory, so cloud saves and achievements
 * are unaffected. Lists ``.exe`` candidates found in the install dir (the
 * auto-detected default labelled, the current one checked), offers a "Browse…"
 * fallback for anything the scan missed, and a "Reset to default" that always
 * gets the user back.
 */
import { FC, useState } from "react";
import { ConfirmModal, DialogButton, Focusable } from "@decky/ui";
import { openFilePicker, FileSelectionType } from "@decky/api";
import { useTranslation } from "react-i18next";
import { FaCheck, FaFolderOpen, FaUndo } from "react-icons/fa";
import { rpcRoutes } from "../../api/rpc-routes";
import { useRPCQuery, useRPCMutation } from "../../api/useRPC";
import { useToast } from "../../hooks/useToast";
import { toastText, headerText } from "../../services/toast-text";

export interface ExecCandidate {
  rel: string;
  name: string;
  is_current: boolean;
  is_default: boolean;
}

export interface ExecList {
  install_dir: string;
  override_active: boolean;
  default_rel: string | null;
  current_rel: string | null;
  candidates: ExecCandidate[];
}

interface SetResult {
  success?: boolean;
  executable?: string | null;
}

interface Props {
  store: string;
  gameId: string;
  gameTitle: string;
  closeModal: () => void;
}

const rowStyle = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 8,
  width: "100%",
} as const;

export const ChangeExecutableModal: FC<Props> = ({
  store,
  gameId,
  gameTitle,
  closeModal,
}) => {
  const { t } = useTranslation();
  const toast = useToast();

  const list = useRPCQuery<[string, string], ExecList>(
    rpcRoutes.listGameExecutables,
    [store, gameId],
  );
  const setExe = useRPCMutation<[string, string, string], SetResult>(
    rpcRoutes.setGameExecutable,
  );
  const resetExe = useRPCMutation<[string, string], SetResult>(
    rpcRoutes.resetGameExecutable,
  );
  const [busy, setBusy] = useState(false);

  const data = list.data;
  const installDir = data?.install_dir ?? "";
  const working = busy || setExe.loading || resetExe.loading;

  const applySet = async (rel: string) => {
    setBusy(true);
    try {
      const res = await setExe.mutate(store, gameId, rel);
      if (res && res.success !== false) {
        toast.success(
          headerText("toasts.header.executable"),
          toastText("toasts.exeChanged"),
        );
        await list.refetch();
      } else {
        toast.error(
          headerText("toasts.header.executable"),
          toastText("toasts.exeChangeFailed"),
        );
      }
    } catch {
      toast.error(
        headerText("toasts.header.executable"),
        toastText("toasts.exeChangeFailed"),
      );
    } finally {
      setBusy(false);
    }
  };

  const applyReset = async () => {
    setBusy(true);
    try {
      const res = await resetExe.mutate(store, gameId);
      if (res && res.success !== false) {
        toast.success(
          headerText("toasts.header.executable"),
          toastText("toasts.exeReset"),
        );
        await list.refetch();
      } else {
        toast.error(
          headerText("toasts.header.executable"),
          toastText("toasts.exeChangeFailed"),
        );
      }
    } finally {
      setBusy(false);
    }
  };

  const browse = async () => {
    if (!installDir) return;
    try {
      // NOTE: do NOT pass a RegExp `filter` — Decky forwards it to the Python
      // backend (`utilities/filepicker_ls`), and a RegExp can't cross the
      // JS→Python RPC bridge (it serialises to `{}`), which makes the backend
      // return an empty listing. The `extensions` array IS serialisable and
      // drives both the ".exe / All Files" dropdown and the server-side filter.
      // `includeFolders: true` lets the user navigate into subdirectories.
      const res = await openFilePicker(
        FileSelectionType.FILE,
        installDir,
        true, // includeFiles
        true, // includeFolders (navigate; was false → no folders shown)
        undefined, // filter — see note above
        ["exe"], // extensions: dropdown + server-side filter
        false, // showHiddenFiles
        true, // allowAllFiles (escape hatch to see everything)
      );
      const abs = res?.realpath || res?.path;
      if (!abs) return;
      const base = installDir.endsWith("/") ? installDir : `${installDir}/`;
      const rel = abs.startsWith(base) ? abs.slice(base.length) : abs;
      await applySet(rel);
    } catch {
      // user cancelled the picker — no-op
    }
  };

  return (
    <ConfirmModal
      strTitle={t("play.exe.title", { game: gameTitle })}
      bAlertDialog
      strOKButtonText={t("common.close")}
      onOK={closeModal}
      onCancel={closeModal}
    >
      <div style={{ marginBottom: 8, opacity: 0.8, fontSize: "0.9em" }}>
        {t("play.exe.subtitle")}
      </div>

      {list.loading && <div>{t("common.loading")}</div>}
      {list.error && <div>{t("play.exe.unavailable")}</div>}

      <Focusable style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {data?.candidates.map((c) => (
          <DialogButton
            key={c.rel}
            disabled={working}
            onClick={() => void applySet(c.rel)}
            style={{ padding: "8px 12px" }}
          >
            <div style={rowStyle}>
              <span
                style={{
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {c.name}
                {c.is_default ? ` ${t("play.exe.defaultTag")}` : ""}
                <span
                  style={{
                    opacity: 0.5,
                    marginInlineStart: 8,
                    fontSize: "0.8em",
                  }}
                >
                  {c.rel}
                </span>
              </span>
              {c.is_current && <FaCheck />}
            </div>
          </DialogButton>
        ))}
      </Focusable>

      <Focusable style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <DialogButton
          disabled={working || !installDir}
          onClick={() => void browse()}
        >
          <FaFolderOpen style={{ marginInlineEnd: 8 }} />
          {t("play.exe.browse")}
        </DialogButton>
        {data?.override_active && (
          <DialogButton disabled={working} onClick={() => void applyReset()}>
            <FaUndo style={{ marginInlineEnd: 8 }} />
            {t("play.exe.resetButton")}
          </DialogButton>
        )}
      </Focusable>
    </ConfirmModal>
  );
};
