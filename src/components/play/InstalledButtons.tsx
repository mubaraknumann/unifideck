/**
 * InstalledButtons — Play section variant for an installed
 * Unifideck game.
 *
 * Single horizontal row inside {@link PlayShell} :
 *
 *   Idle:    [ Play ]     Space Required · Last Played      [ 🎮 ] [ ⚙ ] [ ✕ ]
 *   Running: [ Resume ] [ ✕ ]   Space Required · Last Played [ 🎮 ] [ ⚙ ] [ ✕ ]
 *   Update:  [ Update ]  Space Required · Last Played      [ 🎮 ] [ ⚙ ] [ ✕ ]
 *
 * Running detection polls Steam's per-client ``display_status``
 * every 2 s (4 = running, 1 = launching). Update detection is a
 * reactive read of ``UpdateStore`` via ``useGameUpdate`` — the
 * backend sweep fills it, so no per-mount RPC fires here.
 */
import { FC, useCallback, useEffect, useState } from "react";
import { DialogButton, showModal } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaPlay, FaSyncAlt, FaTimes, FaTrash } from "react-icons/fa";
import { SteamControllerIcon, SteamGearIcon } from "../shared";
import { useGameInfo } from "../../hooks/useGameInfo";
import { useGameActions } from "../../hooks/useGameActions";
import { useGameUpdate } from "../../hooks/useGameUpdate";
import { useToast } from "../../hooks/useToast";
import { SteamBridge } from "../../lib/steam-bridge";
import { openNativeAppManageMenu } from "../../utils/nativeAppMenu";
import { UninstallConfirmModal } from "../modals/UninstallConfirmModal";
import { UpdateAvailableModal } from "../modals/UpdateAvailableModal";
import { CloudSaveButton } from "./CloudSaveButton";
import {
  PlayShell,
  MetaInline,
  IconGroup,
  actionBtnStyle,
  iconBtnStyle,
  actionBtnClass,
  iconBtnClass,
  controllerBtnClass,
} from "./PlayMeta";

interface Props {
  appId: number;
  bridge?: SteamBridge;
}

const defaultBridge = new SteamBridge();
const RUNNING_POLL_MS = 2000;
const STEAM_STATUS_RUNNING = 4;
const STEAM_STATUS_LAUNCHING = 1;

function readDisplayStatus(appId: number): number | undefined {
  const store = (
    window as unknown as {
      appStore?: {
        m_mapApps?: {
          get?: (
            id: number,
          ) =>
            | { local_per_client_data?: { display_status?: number } }
            | undefined;
        };
      };
    }
  ).appStore;
  const app = store?.m_mapApps?.get?.(appId);
  return app?.local_per_client_data?.display_status;
}

function openControllerConfig(appId: number): void {
  (
    window as unknown as {
      SteamClient?: {
        Apps?: { ShowControllerConfigurator?: (id: number) => void };
      };
    }
  ).SteamClient?.Apps?.ShowControllerConfigurator?.(appId);
}

function openAppSettings(appId: number): void {
  (
    window as unknown as {
      SteamClient?: {
        Apps?: { OpenAppSettingsDialog?: (id: number, page: string) => void };
      };
    }
  ).SteamClient?.Apps?.OpenAppSettingsDialog?.(appId, "general");
}

export const InstalledButtons: FC<Props> = ({
  appId,
  bridge = defaultBridge,
}) => {
  const { t } = useTranslation();
  const { data: game, loading } = useGameInfo(appId);
  const actions = useGameActions(bridge);
  const toast = useToast();
  const [isRunning, setIsRunning] = useState(false);
  const gameStore = game?.store;
  const gameId = game?.id;
  // Read-only view of the backend sweep's result — already in memory, so
  // this costs nothing and cannot delay the button the way the old
  // inline per-game update scan did (5-10 s for Epic, because
  // legendary logs in and refreshes its asset manifest first).
  const hasUpdate = useGameUpdate(gameStore, gameId);

  // NOTE: we deliberately do NOT touch Steam's Force-Compatibility here.
  // This used to capture it into proton_settings.json and clear it so
  // RunGame wouldn't wrap our launcher in Proton — but clearing it meant
  // the launcher could never read the user's ACTUAL selection at launch
  // time (only a copy that went stale whenever the capture/restore dance
  // was interrupted), so switching Proton in Steam's dialog appeared to do
  // nothing for some games and work for others purely by timing.
  // ``config.vdf``'s CompatToolMapping is now the single source of truth,
  // read by ``selector.select_proton_version``; the double-Proton problem
  // the clearing existed to avoid is handled properly at the umu spawn
  // point by ``launcher.proton.infrastructure.container_escape``.

  // Running-state poll (2 s).
  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      const status = readDisplayStatus(appId);
      if (status === undefined) return;
      setIsRunning(
        status === STEAM_STATUS_RUNNING || status === STEAM_STATUS_LAUNCHING,
      );
    };
    tick();
    const id = window.setInterval(tick, RUNNING_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [appId]);

  const onUpdate = useCallback(async () => {
    if (!game) return;
    try {
      const res = await actions.update(appId, gameStore, gameId);
      if (res?.success) {
        toast.success(t("toasts.updateQueued"));
      } else {
        toast.error(t("toasts.updateFailed"), res?.error ?? "");
      }
    } catch (e) {
      toast.error(t("toasts.updateFailed"), String(e));
    }
  }, [actions, appId, game, gameId, gameStore, t, toast]);

  const onResume = useCallback(() => {
    actions.launch(appId);
  }, [actions, appId]);

  // Launching a game with a pending update asks first rather than
  // blocking: most single-player titles run fine a build behind, but an
  // online game will simply refuse to connect, and nothing on the launch
  // path used to tell the user that was why. Reads cached state only —
  // no network call, so a game with no update launches straight through.
  //
  // Defence in depth: the button below already renders Update INSTEAD of
  // Play whenever we know about one, so this normally never fires. It
  // covers the window where the sweep's event lands between render and
  // press — without it, that press would silently launch a stale build.
  const onPlay = useCallback(() => {
    if (!game) return;
    if (!hasUpdate) {
      actions.launch(appId);
      return;
    }
    showModal(
      <UpdateAvailableModal
        gameTitle={game.title}
        onUpdate={onUpdate}
        onPlayAnyway={() => actions.launch(appId)}
        closeModal={() => {}}
      />,
    );
  }, [actions, appId, game, hasUpdate, onUpdate]);

  const onStop = useCallback(() => {
    actions.terminate(appId);
  }, [actions, appId]);

  const onUninstall = useCallback(() => {
    if (!game) return;
    showModal(
      <UninstallConfirmModal
        gameId={appId}
        gameTitle={game.title}
        onConfirm={async (deletePrefix) => {
          const r = await actions.uninstall(appId, deletePrefix);
          if (r?.success) toast.success(t("toasts.uninstallDone"));
        }}
        closeModal={() => {}}
      />,
    );
  }, [actions, appId, game, t, toast]);

  const primaryButtons = (() => {
    if (isRunning) {
      return (
        <>
          {/* Resume, not onPlay: the game is ALREADY running, so the
              pending-update prompt would be nonsense here — there is
              nothing left to decide, and updating over a running game
              is not something we want to offer. */}
          <DialogButton
            className={actionBtnClass("unifideck-resume-btn")}
            disabled={loading}
            onClick={onResume}
            style={actionBtnStyle}
          >
            <FaPlay /> {t("play.resume")}
          </DialogButton>
          <DialogButton
            className={iconBtnClass("unifideck-stop-btn")}
            onClick={onStop}
            style={iconBtnStyle}
            aria-label={t("play.stop")}
          >
            <FaTimes />
          </DialogButton>
        </>
      );
    }
    if (hasUpdate) {
      return (
        <DialogButton
          className={actionBtnClass("unifideck-update-btn")}
          disabled={loading || actions.isWorking}
          onClick={onUpdate}
          style={actionBtnStyle}
        >
          <FaSyncAlt /> {t("play.update")}
        </DialogButton>
      );
    }
    return (
      <DialogButton
        className={actionBtnClass("unifideck-play-btn")}
        disabled={loading}
        onClick={onPlay}
        style={actionBtnStyle}
      >
        <FaPlay /> {t("play.play")}
      </DialogButton>
    );
  })();

  return (
    // autoFocus is intentional: claims gamepad focus for the primary action
    // eslint-disable-next-line jsx-a11y/no-autofocus
    <PlayShell autoFocus>
      {primaryButtons}
      <MetaInline
        sizeBytes={game?.size_bytes}
        showLastPlayed
        appId={appId}
        store={game?.store}
        gameId={game?.id}
        installed
      />
      <IconGroup>
        {game && (
          <CloudSaveButton
            store={game.store}
            gameId={game.id}
            gameTitle={game.title}
          />
        )}
        <DialogButton
          className={controllerBtnClass()}
          style={iconBtnStyle}
          onClick={() => openControllerConfig(appId)}
          aria-label={t("playButton.controllerConfig")}
        >
          <SteamControllerIcon />
        </DialogButton>
        <DialogButton
          className={iconBtnClass()}
          style={iconBtnStyle}
          onClick={(e) => {
            // Open Steam's native app menu (Manage / Properties / …),
            // matching the native gear; fall back to Properties directly.
            if (!openNativeAppManageMenu(e?.currentTarget as HTMLElement)) {
              openAppSettings(appId);
            }
          }}
          aria-label={t("playButton.appSettings")}
        >
          <SteamGearIcon />
        </DialogButton>
        <DialogButton
          className={iconBtnClass()}
          style={iconBtnStyle}
          disabled={loading || actions.isWorking}
          onClick={onUninstall}
          aria-label={t("play.uninstall")}
        >
          <FaTrash />
        </DialogButton>
      </IconGroup>
    </PlayShell>
  );
};
