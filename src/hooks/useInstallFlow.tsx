/**
 * useInstallFlow — orchestrates the install handshake.
 *
 * Wraps `useGameActions.install` with the per-store side
 * quests the user may have to walk through before the
 * download is queued :
 *  - Storage picker: `pickStorageForInstall` always runs first
 *    so the user chooses where the game lands.
 *  - GOG / Epic / EA: fetch the store's install languages, prompt the
 *    user via `<LanguageSelectModal>` when more than one is
 *    offered. Epic only reports languages for legendary's
 *    Selective Downloads titles, where the language packs are
 *    separate downloads; everything else comes back empty and
 *    installs without a prompt. EA reports the build's locales
 *    (claiming an EA Play game first, as EA serves build details
 *    only for owned games).
 *  - A game paid for but not yet claimed (an itch.io bundle game, tag
 *    `claim_required`): the claim page opens first
 *    (`services/store/claim-flow.tsx`). Also run when `install_game`
 *    answers `*_claim_required`, for a tile whose tags predate the claim
 *    rule, then the install is retried once.
 *  - An Xbox PC title (tag `xbox_pc`): the setup modal opens first when
 *    the live status says setup is incomplete (`needsXboxPcSetup`): it is
 *    finished in place, with no dialog (`linkXboxPc`). When
 *    `install_game` refuses with a setup code (`xbox_pc_disabled`,
 *    `_runtime_missing`, `_not_signed_in`), setup runs and the install is
 *    retried once. No language picker: Xodus picks the package. When the
 *    game has DLC over 1 GiB, `DlcSelectModal` asks which to install
 *    first (small DLC always installs with the game).
 *  - Other stores: pass straight through.
 */
import { useCallback, useState } from "react";
import { showModal } from "@decky/ui";
import i18n from "i18next";
import { useRPC } from "../api/useRPC";
import { rpcRoutes } from "../api/rpc-routes";
import { useGameActions } from "./useGameActions";
import { useStorageConfig } from "./useStorageConfig";
import { LanguageSelectModal } from "../components/modals/LanguageSelectModal";
import { pickStorageForInstall } from "../components/modals/PickStorageModal";
import type { Game, Result } from "../types/api";
import type { StorageLocationsResponse } from "../types/downloads";
import { storeHasCapability } from "./useStoreCapability";
import {
  claimBeforeInstall,
  isClaimRequiredError,
  needsClaim,
} from "../services/store/claim-flow";
import {
  isXboxPcRow,
  isXboxPcSetupError,
  needsXboxPcSetup,
  runXboxPcSetup,
} from "../services/store/xbox-pc-flow";
import { xboxPcSnapshot } from "./useXboxPcStatus";
import { downloadSizeBytes } from "./useGameSize";
import {
  dlcListForInstall,
  pickDlcForInstall,
} from "../services/store/xbox-pc-dlc";

/** Steam bridge shape — same minimal surface useGameActions
 *  consumes. */
interface SteamBridgeShape {
  runGame(appId: number): void;
  terminateApp(appId: string, force?: boolean): void;
}

/** Languages response from `get_gog_game_languages` /
 *  `get_install_languages`. Locale codes the modal maps to display
 *  labels; a store whose options carry their own names (Epic's SDL
 *  configs) also sends `labels`. */
interface LanguagesResponse {
  success: boolean;
  languages: string[];
  labels?: Record<string, string>;
  /** With one option or none, install in the UI language rather than the
   *  store's default (EA: Touchup writes it for the game). */
  follow_ui?: boolean;
}

/**
 * Bundle returned by {@link useInstallFlow} — `start(game)`
 * kicks the install and resolves with the RPC result so the
 * caller can toast success / failure.
 */
export interface UseInstallFlowResult {
  isWorking: boolean;
  start: (game: Game) => Promise<Result | null>;
}

/**
 * Wraps install with the per-store prompts required to
 * complete the handshake. Returns `null` if the user
 * cancels a prompt.
 */
export function useInstallFlow(bridge: SteamBridgeShape): UseInstallFlowResult {
  const actions = useGameActions(bridge);
  const getGogLangs = useRPC<[string], LanguagesResponse>(
    rpcRoutes.getGogGameLanguages,
  );
  const getStoreLangs = useRPC<[string, string], LanguagesResponse>(
    rpcRoutes.getInstallLanguages,
  );
  const getStorageLocations = useRPC<[], StorageLocationsResponse>(
    rpcRoutes.getStorageLocations,
  );
  const { locations, defaultLocation, setCustomPath } = useStorageConfig();
  const [working, setWorking] = useState(false);

  const start = useCallback(
    async (game: Game): Promise<Result | null> => {
      setWorking(true);
      try {
        if (needsClaim(game)) {
          const claim = await claimBeforeInstall(game);
          if (!claim?.success) return claim;
        }
        if (needsXboxPcSetup(game, xboxPcSnapshot().status)) {
          if (!(await runXboxPcSetup())) return null;
        }
        const install = async (opts: {
          storage: string;
          language?: string;
        }): Promise<Result | null> => {
          const args = { ...opts, title: game.title };
          const result = await actions.install(
            game.store,
            game.store_game_id,
            args,
          );
          const retry = () =>
            actions.install(game.store, game.store_game_id, args);
          if (isXboxPcSetupError(result.error)) {
            // The status was stale (or not loaded): setup, then once more.
            if (!(await runXboxPcSetup())) return result;
            return retry();
          }
          if (!isClaimRequiredError(result.error)) return result;
          const claim = await claimBeforeInstall(game);
          if (!claim?.success) return claim ?? result;
          return retry();
        };
        // `useStorageConfig` fetches once at row-mount time, which can be
        // long before the user opens this picker (e.g. an SD card inserted
        // after the library grid rendered). Re-fetch here so newly
        // inserted/removed external drives are always reflected; fall back
        // to the cached values on failure rather than blocking the install.
        let currentLocations = locations;
        let currentDefault = defaultLocation;
        try {
          const fresh = await getStorageLocations();
          currentLocations = fresh.locations;
          currentDefault = fresh.default;
        } catch (e) {
          console.log(
            "[useInstallFlow] storage location refresh failed, using cached list",
            e,
          );
        }

        // The row often carries no size (0): read the download size the
        // game page shows ("Space required") instead.
        let baseBytes = game.size_bytes ?? 0;
        if (baseBytes <= 0 && game.app_id) {
          baseBytes = await downloadSizeBytes(game.app_id);
        }

        // Xbox PC: large DLC is chosen before the storage picker, so the
        // free-space check counts the game plus every DLC that will download.
        let dlcBytes = 0;
        if (isXboxPcRow(game)) {
          const list = await dlcListForInstall(game);
          const extra =
            list == null
              ? 0
              : await pickDlcForInstall(
                  game.store_game_id,
                  game.title,
                  list,
                  baseBytes,
                );
          if (extra == null) return null;
          dlcBytes = extra;
        }

        console.log("[useInstallFlow] opening storage picker for", game.title);
        const total = baseBytes + dlcBytes;
        const picked = await pickStorageForInstall(
          game.title,
          total > 0 ? total : undefined,
          currentLocations,
          currentDefault,
          setCustomPath,
        );
        if (!picked) {
          console.log("[useInstallFlow] storage picker cancelled");
          return null;
        }
        const { storage, customPath } = picked;
        console.log(
          "[useInstallFlow] picked storage=%s customPath=%s",
          storage,
          customPath ?? "none",
        );

        // Whether a store has a picker is the backend's
        // `has_language_picker` capability, never a list here. GOG keeps
        // its own route; every other picker store answers the generic one.
        const hasPicker =
          !isXboxPcRow(game) &&
          storeHasCapability(game.store, "has_language_picker");
        if (!hasPicker) {
          console.log(
            "[useInstallFlow] installing %s/%s with storage=%s",
            game.store,
            game.store_game_id,
            storage,
          );
          return await install({ storage });
        }
        const langs = await (game.store === "gog"
          ? getGogLangs(game.store_game_id)
          : getStoreLangs(game.store, game.store_game_id)
        ).catch(() => null);
        const list = langs?.languages ?? [];
        if (list.length <= 1) {
          const language =
            list[0] ?? (langs?.follow_ui ? i18n.language : undefined);
          return await install({ language, storage });
        }
        const language = await pickLanguageViaModal(
          game.title,
          list,
          langs?.labels,
        );
        if (!language) return null;
        return await install({ language, storage });
      } finally {
        setWorking(false);
      }
    },
    [
      actions,
      getGogLangs,
      getStoreLangs,
      getStorageLocations,
      locations,
      defaultLocation,
      setCustomPath,
    ],
  );

  return { isWorking: working || actions.isWorking, start };
}

/** Promise-wrapped showModal that resolves with the picked
 *  language (or null on cancel). Pre-selects the active UI
 *  language (which reflects the "auto" preference resolved to the
 *  system language) when the game offers it. */
function pickLanguageViaModal(
  title: string,
  languages: string[],
  labels?: Record<string, string>,
): Promise<string | null> {
  return new Promise((resolve) => {
    let confirmed = false;
    const handle = showModal(
      <LanguageSelectModal
        gameTitle={title}
        languages={languages}
        labels={labels}
        preferredTag={i18n.language}
        onConfirm={(lang) => {
          confirmed = true;
          resolve(lang);
        }}
        closeModal={() => {
          handle?.Close();
          if (!confirmed) resolve(null);
        }}
      />,
    );
  });
}
