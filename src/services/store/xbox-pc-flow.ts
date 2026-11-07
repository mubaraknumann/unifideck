/**
 * The install flow's Xbox PC side step.
 *
 * `useInstallFlow` calls {@link runXboxPcSetup} before the storage picker
 * when `needsXboxPcSetup` says setup is incomplete, and again (then retries
 * the install once) when `install_game` refuses with a setup code. There is
 * no setup dialog: with Microsoft connected, `linkXboxPc` installs the
 * components and signs in by itself; without it, a toast says to connect
 * Microsoft first. The predicates live in `lib/xbox-pc-setup` and are
 * re-exported here so the install flow has one import for the whole step.
 */
import { toastText } from "../toast-text";
import { xboxPcStore } from "../../stores/xbox-pc-store";
import { xboxPcToast } from "./xbox-pc-actions";
import { linkXboxPc, waitForRuntime } from "./xbox-pc-link";

export {
  isXboxPcRow,
  isXboxPcSetupError,
  needsXboxPcSetup,
} from "../../lib/xbox-pc-setup";

/** Finish Xbox PC setup for an install. Resolves true when ready. */
export async function runXboxPcSetup(): Promise<boolean> {
  // Ask again before refusing: the snapshot may predate the sign-in check.
  if (xboxPcStore.getSnapshot().status?.xcloud_signed_in === false) {
    await xboxPcStore.refresh();
  }
  if (xboxPcStore.getSnapshot().status?.xcloud_signed_in === false) {
    xboxPcToast(toastText("toasts.xboxPcError.microsoftRequired"));
    return false;
  }
  // The link can finish on the backend alone. An install needs the runtime too.
  return (await linkXboxPc()) && waitForRuntime();
}
