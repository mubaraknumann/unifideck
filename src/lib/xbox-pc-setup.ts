/**
 * xbox-pc-setup: pure predicates over the Xbox PC status.
 *
 * Installing an Xbox PC title needs four things in place: the feature
 * turned on (after the unofficial notice), the Microsoft (xCloud) sign-in,
 * the components (our Xodus build and runtime), and the Xbox PC sign-in.
 * The install flow, the setup modal and the sign-in flow all ask the same
 * questions about that, so the answers live here, with no UI imports.
 *
 * The backend still re-checks every install (`install_precondition`), so a
 * stale status here costs at most one extra round through the setup modal.
 */
import type { Game, XboxPcStatus } from "../types/api";

/** Account states that block an install until the user signs in. */
const BLOCKING_ACCOUNT = new Set(["signed_out", "error", "mismatch"]);

/**
 * Runtime states a game install and the sign-in can use. Mirrors the
 * backend's `USABLE_RUNTIME` (`stores/microsoft/xodus/install_steps.py`):
 * `outdated` means a newer build is in the source folder, not that the
 * installed one is broken. Updating is offered, never required.
 */
const USABLE_RUNTIME = new Set(["installed", "outdated"]);

/** Whether the installed components can run installs and the sign-in. */
export function runtimeUsable(state: string): boolean {
  return USABLE_RUNTIME.has(state);
}

/** Whether `game` is a title Unifideck installs through Xodus. */
export function isXboxPcRow(game: Pick<Game, "store_tags">): boolean {
  return game.store_tags?.includes("xbox_pc") ?? false;
}

/**
 * Whether setup is complete enough to try an install.
 *
 * `unknown` and `unverified` accounts do not block: the backend answers
 * with `xbox_pc_not_signed_in` if it really is signed out, and the flow
 * then runs setup and retries. Sending a user through sign-in on a guess
 * costs them an Authenticator push (I-009).
 */
export function isSetupReady(status: XboxPcStatus | null): boolean {
  if (!status) return false;
  return (
    status.enabled &&
    status.xcloud_signed_in &&
    runtimeUsable(status.runtime.state) &&
    !BLOCKING_ACCOUNT.has(status.account.state)
  );
}

/**
 * Whether Install on `game` should open the setup modal first.
 *
 * Only for an `xbox_pc` row, and only when the status is known: a missing
 * status (still loading, or failed) leaves the decision to the backend's
 * refusal and the retry in `useInstallFlow`.
 */
export function needsXboxPcSetup(
  game: Pick<Game, "store_tags">,
  status: XboxPcStatus | null,
): boolean {
  if (!isXboxPcRow(game) || !status) return false;
  return !isSetupReady(status);
}

/** Whether an `install_game` refusal is one setup can fix. */
export function isXboxPcSetupError(error: string | undefined): boolean {
  return /^xbox_pc_(disabled|runtime_missing|not_signed_in)\b/.test(
    error ?? "",
  );
}

/**
 * Whether `status` ends a sign-in, and how. Null while it is running.
 *
 * `unverified` counts as signed in: the credentials are stored, only the
 * gamertag cross-check could not run, and the backend rechecks it later.
 */
export function signInOutcome(
  status: XboxPcStatus | null,
): { ok: boolean; error?: string } | null {
  if (!status) return { ok: false, error: "status_unavailable" };
  const { state, error } = status.account;
  if (state === "signing_in") return null;
  if (state === "signed_in" || state === "unverified") return { ok: true };
  if (state === "mismatch") return { ok: false, error: "account_mismatch" };
  return { ok: false, error: error || "window_closed" };
}
