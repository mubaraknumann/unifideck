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
 *   2. open the Xbox PC sign-in window if Microsoft was connected the old way
 *      (OAuth) before the toggle was on. The sign-in only needs the backend,
 *      which an install fetches first, so the window opens seconds after the
 *      toggle while the runtime keeps downloading. Waiting for the runtime
 *      would open it minutes later, when nobody expects it.
 *
 * With the toggle on, connecting Microsoft in Store Connections IS the Xbox
 * PC sign-in (backend `single_signin`), and xCloud runs on its ticket: one
 * sign-in, one window.
 *
 * It runs when the toggle turns on and after a Microsoft connect. It also runs
 * at boot and on a manual sync with `auto: true`, so a user who never finished
 * the sign-in gets the window again: there is no sign-in button to go back to.
 * `auto` only ever opens it for a `signed_out` account with no game running.
 * Signing out of Microsoft signs out of both (backend `on_microsoft_logout`).
 * Failures toast (rule 8); nothing here opens a settings panel.
 */
import { Router } from "@decky/ui";
import { runtimeUsable } from "../../lib/xbox-pc-setup";
import { xboxPcStore } from "../../stores/xbox-pc-store";
import { signInXboxPc } from "../auth/xbox-pc-sign-in";
import { toastXboxPcError } from "./xbox-pc-actions";
import type { XboxPcStatus } from "../../types/api";

/** Account states that already hold usable Xodus credentials. */
const LINKED = new Set(["signed_in", "unverified"]);

let running: { done: Promise<boolean>; auto: boolean } | null = null;

/** The current status, or null while unknown. */
function current(): XboxPcStatus | null {
  return xboxPcStore.getSnapshot().status;
}

/** Wait until the install ends; true when `ready` holds for the runtime row. */
function waitForInstall(
  ready: (runtime: XboxPcStatus["runtime"]) => boolean,
): Promise<boolean> {
  return new Promise((resolve) => {
    const check = (): void => {
      const runtime = current()?.runtime;
      if (runtime && ready(runtime)) {
        unsubscribe();
        resolve(true);
      } else if (runtime?.state !== "installing") {
        unsubscribe();
        resolve(false);
      }
    };
    const unsubscribe = xboxPcStore.subscribe(check);
    check();
  });
}

/** Whether the sign-in can run with this runtime row. */
function backendUsable(runtime: XboxPcStatus["runtime"]): boolean {
  return runtime.backend_ready === true || runtimeUsable(runtime.state);
}

/** Wait until games can install: the whole install, not only the backend. */
export function waitForRuntime(): Promise<boolean> {
  return waitForInstall((runtime) => runtimeUsable(runtime.state));
}

/** Start the install. A refusal toasts, except "it is already running". */
async function startInstall(): Promise<boolean> {
  const r = await xboxPcStore.installRuntime();
  if (!r.ok && r.error !== "install_in_progress") {
    toastXboxPcError("component install", r.error);
    return false;
  }
  // Progress only arrives as a polled event, so the status can still say
  // `missing` here, and a wait would give up at once and skip the sign-in.
  await xboxPcStore.refetch();
  return true;
}

/**
 * Install the components unless they are usable, and resolve once the backend
 * is there. The store toasts the end of an install (installed or failed), so
 * only a refusal toasts here.
 */
async function ensureBackend(status: XboxPcStatus): Promise<boolean> {
  const { state } = status.runtime;
  if (state === "outdated") {
    // A newer build is in the source folder. The installed one keeps
    // working, so update in the background: the swap is atomic, and games
    // or downloads already running keep the old copy until they end.
    void startInstall();
    return true;
  }
  if (runtimeUsable(state)) return true;
  if (state !== "installing" && !(await startInstall())) return false;
  return waitForInstall(backendUsable);
}

/** A game on screen is never interrupted by a window it did not ask for. */
function gameRunning(): boolean {
  return Router.MainRunningApp != null;
}

async function link(auto: boolean): Promise<boolean> {
  await xboxPcStore.refetch();
  const status = current();
  if (!status?.enabled) return false;
  // The components first, even before Microsoft is connected: with Xbox PC
  // on, connecting Microsoft IS the Xbox PC sign-in, which needs them.
  if (!(await ensureBackend(status))) return false;
  // Ask again before giving up: right after a plugin load the first answer
  // can predate the backend's own sign-in check.
  if (!current()?.xcloud_signed_in) await xboxPcStore.refresh();
  if (!current()?.xcloud_signed_in) return false;
  let account = current()?.account.state;
  if (account === "unknown") {
    await xboxPcStore.refresh();
    account = current()?.account.state;
  }
  if (account && LINKED.has(account)) return true;
  if (account === "signing_in") return false;
  if (auto && (account !== "signed_out" || gameRunning())) return false;
  // Microsoft was connected the old way (OAuth, before the toggle was on):
  // one Xbox PC sign-in, once. From then on it is the only sign-in.
  return signInXboxPc();
}

/**
 * Bring Xbox PC up to "ready for the sign-in" and open it when needed.
 * Resolves true once linked; the runtime may still be installing, so an install
 * also needs {@link waitForRuntime}. One run at a time; callers share it, and
 * a caller that asked outright is not lost by joining an `auto` run.
 */
export function linkXboxPc(opts: { auto?: boolean } = {}): Promise<boolean> {
  const auto = opts.auto ?? false;
  if (running) {
    if (!auto && running.auto) {
      return running.done.then(() => linkXboxPc(opts));
    }
    return running.done;
  }
  const done = link(auto).finally(() => {
    running = null;
  });
  running = { done, auto };
  return done;
}
