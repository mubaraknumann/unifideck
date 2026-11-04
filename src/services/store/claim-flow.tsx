/**
 * Claim a paid-for game on the store's site before installing it.
 *
 * itch.io bundle games are listed before they are claimed (backend
 * `stores/itch/bundles.py`). The user decided the claim is theirs to make,
 * so instead of claiming through the API the flow:
 *
 *   1. asks the backend to arm the claim page (`begin_store_claim`), which
 *      the next shop window opens instead of the store's front page;
 *   2. explains what to do (`ClaimRequiredModal`) and opens the shop window;
 *   3. waits for the window to close, then asks the backend whether the
 *      game is now owned (`finish_store_claim`, which re-reads the owned
 *      keys).
 *
 * Resolves `null` when the user cancels, a failed `Result` with a
 * translated message when the claim did not happen, and `{success: true}`
 * when the install may go ahead.
 */
import { call } from "@decky/api";
import { showModal } from "@decky/ui";
import i18n from "i18next";
import { rpcRoutes } from "../../api/rpc-routes";
import { ChromiumInstallModal } from "../../components/modals/ChromiumInstallModal";
import { ClaimRequiredModal } from "../../components/modals/ClaimRequiredModal";
import { watchAppStopped } from "../../lib/steam-bridge/shortcut-types";
import { BROWSER_MISSING, openStorefrontWindow } from "./StorefrontLauncher";
import type { Game, Result, StoreId } from "../../types/api";

interface BeginClaimResponse {
  success: boolean;
  error?: string;
  url?: string;
  bundle_title?: string;
}

interface FinishClaimResponse {
  success: boolean;
  claimed?: boolean;
}

/** Whether `game` must be claimed before it can install. */
export function needsClaim(game: Pick<Game, "store_tags">): boolean {
  return game.store_tags?.includes("claim_required") ?? false;
}

/** Whether an `install_game` error is the backend's "claim it first". */
export function isClaimRequiredError(error: string | undefined): boolean {
  return /^[a-z]+_claim_required\b/.test(error ?? "");
}

const failed = (key: string): Result => ({
  success: false,
  error: i18n.t(key),
});

function confirmViaModal(game: Game, bundleTitle: string): Promise<boolean> {
  return new Promise((resolve) => {
    let confirmed = false;
    const handle = showModal(
      <ClaimRequiredModal
        gameTitle={game.title}
        bundleTitle={bundleTitle}
        onConfirm={() => {
          confirmed = true;
          resolve(true);
        }}
        closeModal={() => {
          handle?.Close();
          if (!confirmed) resolve(false);
        }}
      />,
    );
  });
}

function windowClosed(appId: number): Promise<void> {
  return new Promise((resolve) => {
    const stop = watchAppStopped(appId, () => {
      stop();
      resolve();
    });
  });
}

const begin = (store: StoreId, gameId: string) =>
  call<[StoreId, string], BeginClaimResponse>(
    rpcRoutes.beginStoreClaim,
    store,
    gameId,
  );

/** Walk the user through claiming `game`; see the module docstring. */
export async function claimBeforeInstall(game: Game): Promise<Result | null> {
  const store = game.store;
  const id = game.store_game_id;
  const first = await begin(store, id).catch(() => null);
  if (!first?.success) return failed("claimModal.openFailed");
  if (!(await confirmViaModal(game, first.bundle_title ?? ""))) return null;
  // Re-armed: the page is one-time and expires, and the modal may have
  // sat open for a while.
  await begin(store, id).catch(() => null);
  const launched = await openStorefrontWindow(store);
  if (launched.error === BROWSER_MISSING) {
    showModal(<ChromiumInstallModal closeModal={() => {}} />);
    return null;
  }
  if (!launched.success || !launched.app_id) {
    return failed("claimModal.openFailed");
  }
  await windowClosed(launched.app_id);
  const checked = await call<[StoreId, string], FinishClaimResponse>(
    rpcRoutes.finishStoreClaim,
    store,
    id,
  ).catch(() => null);
  return checked?.claimed ? { success: true } : failed("claimModal.notClaimed");
}
