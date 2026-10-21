/**
 * The install flow's Xbox PC side step: open the setup modal and wait.
 *
 * `useInstallFlow` calls {@link runXboxPcSetup} before the storage picker
 * when `needsXboxPcSetup` says setup is incomplete, and again (then retries
 * the install once) when `install_game` refuses with a setup code. The
 * predicates live in `lib/xbox-pc-setup` and are re-exported here so the
 * install flow has one import for the whole step.
 */
import { showModal } from "@decky/ui";
import { XboxPcSetupModal } from "../../components/modals/XboxPcSetupModal";

export {
  isXboxPcRow,
  isXboxPcSetupError,
  needsXboxPcSetup,
} from "../../lib/xbox-pc-setup";

/**
 * Open the setup modal. Resolves true when the user continues with setup
 * done, false when they close it.
 */
export function runXboxPcSetup(gameTitle?: string): Promise<boolean> {
  return new Promise((resolve) => {
    let ready = false;
    const handle = showModal(
      <XboxPcSetupModal
        gameTitle={gameTitle}
        onReady={() => {
          ready = true;
          resolve(true);
        }}
        closeModal={() => {
          handle?.Close();
          if (!ready) resolve(false);
        }}
      />,
    );
  });
}
