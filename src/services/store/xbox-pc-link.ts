/**
 * Xbox PC linking: one Microsoft sign-in serves cloud and PC games.
 *
 * From the user's side there is a single Microsoft account in Store
 * Connections and a single "Enable Experimental Xbox PC Downloads" toggle.
 * Under it Xodus still needs its own device-bound credential: the xCloud
 * OAuth tokens cannot make one. So whenever the toggle is on and Microsoft
 * is connected, {@link linkXboxPc} makes the rest happen by itself:
 *
 *   1. install the components (our Xodus build and runtime) if missing,
 *      or update them in the background when the source has a newer build;
 *   2. run the Xodus sign-in in the same Edge profile. It is single sign-on:
 *      no password, at most an Authenticator approval (I-009).
 *
 * It runs when the toggle turns on, after a Microsoft connect, and at boot
 * (components only: an Edge window never opens unasked at startup).
 * Signing out of Microsoft signs out of both (backend `on_microsoft_logout`).
 * Failures toast (rule 8); nothing here opens a settings panel.
 */
import { runtimeUsable } from "../../lib/xbox-pc-setup";
import { xboxPcStore } from "../../stores/xbox-pc-store";
import { signInXboxPc } from "../auth/xbox-pc-sign-in";
import { toastXboxPcError } from "./xbox-pc-actions";
import type { XboxPcStatus } from "../../types/api";

/** Account states that already hold usable Xodus credentials. */
const LINKED = new Set(["signed_in", "unverified"]);

let running: Promise<boolean> | null = null;

/** The current status, or null while unknown. */
function current(): XboxPcStatus | null {
  return xboxPcStore.getSnapshot().status;
}

/** Wait until the component install ends; true when the runtime is usable. */
function waitForComponents(): Promise<boolean> {
  return new Promise((resolve) => {
    const check = (): void => {
      const state = current()?.runtime.state;
      if (state === "installing") return;
      unsubscribe();
      resolve(runtimeUsable(state ?? "missing"));
    };
    const unsubscribe = xboxPcStore.subscribe(check);
    check();
  });
}

/**
 * Install the components unless they are usable. The store toasts the
 * end of an install (installed or failed), so only a refusal toasts here.
 */
async function ensureComponents(status: XboxPcStatus): Promise<boolean> {
  if (status.runtime.state === "outdated") {
    // A newer build is in the source folder. The installed one keeps
    // working, so update in the background: the swap is atomic, and games
    // or downloads already running keep the old copy until they end.
    void xboxPcStore.installRuntime().then((r) => {
      if (!r.ok && r.error !== "install_in_progress") {
        toastXboxPcError("toasts.xboxPcRuntimeFailed", r.error);
      }
    });
    return true;
  }
  if (runtimeUsable(status.runtime.state)) return true;
  if (status.runtime.state !== "installing") {
    const r = await xboxPcStore.installRuntime();
    if (!r.ok && r.error !== "install_in_progress") {
      toastXboxPcError("toasts.xboxPcRuntimeFailed", r.error);
      return false;
    }
  }
  return waitForComponents();
}

async function link(signIn: boolean): Promise<boolean> {
  await xboxPcStore.refetch();
  const status = current();
  if (!status?.enabled || !status.xcloud_signed_in) return false;
  if (!(await ensureComponents(status))) return false;
  let account = current()?.account.state;
  if (account === "unknown") {
    await xboxPcStore.refresh();
    account = current()?.account.state;
  }
  if (account && LINKED.has(account)) return true;
  if (!signIn || account === "signing_in") return false;
  return signInXboxPc();
}

/**
 * Bring Xbox PC up to "ready" for the connected Microsoft account.
 * Resolves true when ready. One run at a time; callers share it.
 *
 * `signIn: false` stops before the sign-in (used at boot).
 */
export function linkXboxPc(opts: { signIn?: boolean } = {}): Promise<boolean> {
  if (!running) {
    running = link(opts.signIn ?? true).finally(() => {
      running = null;
    });
  }
  return running;
}
