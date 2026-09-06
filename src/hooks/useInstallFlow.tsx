/**
 * useInstallFlow — orchestrates the install handshake.
 *
 * Wraps `useGameActions.install` with the per-store side
 * quests the user may have to walk through before the
 * download is queued :
 *  - Storage picker: `pickStorageForInstall` always runs first
 *    so the user chooses where the game lands.
 *  - GOG / Epic: fetch the store's install languages, prompt the
 *    user via `<LanguageSelectModal>` when more than one is
 *    offered. Epic only reports languages for legendary's
 *    Selective Downloads titles, where the language packs are
 *    separate downloads; everything else comes back empty and
 *    installs without a prompt.
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

/** Steam bridge shape — same minimal surface useGameActions
 *  consumes. */
interface SteamBridgeShape {
  runGame(appId: number): void;
  terminateApp(appId: string, force?: boolean): void;
}

/** Languages response from `get_gog_game_languages` /
 *  `get_epic_game_languages`. GOG returns raw locale codes and the
 *  modal maps them to display labels; Epic's SDL configs carry their
 *  own names, so it also sends `labels`. */
interface LanguagesResponse {
  success: boolean;
  languages: string[];
  labels?: Record<string, string>;
}

/** Which RPC reports a store's language list.
 *
 *  *Whether* a store has one is the backend's `has_language_picker`
 *  capability, not a list here — this map only records which of the two
 *  routes to call, which the payload does not carry. It used to double as
 *  the gate, making it a second copy of a per-store policy (audit register
 *  items 26/31). Keys must stay a subset of the capability set;
 *  `tests/unit/test_store_capabilities.py` pins the backend half. */
const LANGUAGE_ROUTE: Record<string, "gog" | "epic" | undefined> = {
  gog: "gog",
  epic: "epic",
};

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
  const getEpicLangs = useRPC<[string], LanguagesResponse>(
    rpcRoutes.getEpicGameLanguages,
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

        console.log("[useInstallFlow] opening storage picker for", game.title);
        const picked = await pickStorageForInstall(
          game.title,
          game.size_bytes,
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

        const fetchLangs = storeHasCapability(game.store, "has_language_picker")
          ? LANGUAGE_ROUTE[game.store]
          : undefined;
        if (!fetchLangs) {
          console.log(
            "[useInstallFlow] installing %s/%s with storage=%s",
            game.store,
            game.store_game_id,
            storage,
          );
          return await actions.install(game.store, game.store_game_id, {
            storage,
            title: game.title,
          });
        }
        const langs = await (fetchLangs === "gog"
          ? getGogLangs(game.store_game_id)
          : getEpicLangs(game.store_game_id)
        ).catch(() => null);
        const list = langs?.languages ?? [];
        if (list.length <= 1) {
          const language = list[0];
          return await actions.install(game.store, game.store_game_id, {
            language,
            storage,
            title: game.title,
          });
        }
        const language = await pickLanguageViaModal(
          game.title,
          list,
          langs?.labels,
        );
        if (!language) return null;
        return await actions.install(game.store, game.store_game_id, {
          language,
          storage,
          title: game.title,
        });
      } finally {
        setWorking(false);
      }
    },
    [
      actions,
      getGogLangs,
      getEpicLangs,
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
