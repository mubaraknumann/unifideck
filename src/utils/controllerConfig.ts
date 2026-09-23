/**
 * Controller-aware launch wrapper.
 *
 * An earlier programmatic controller-layout approach (driving Steam's
 * Controller Configurator popup) was reverted: it raced Steam's own
 * configurator and threw "Unknown method" errors. The replacement here
 * uses the typed `SteamClient.Input.SetSelectedConfigForApp` setter
 * directly — method names + signature verified against the Steam UI
 * bundle (`steamui/*.js`), and the call confirmed working in Gaming Mode.
 *
 * Two layouts are applied, both off the live template stream:
 *   • Auth/login window → "Web Browser" (keyboard/mouse) so the in-prefix
 *     login UI is navigable — see `applyWebBrowserLayout`.
 *   • xCloud game launch → "Gamepad With Joystick Trackpad" so the Deck
 *     controller drives the streamed game as an Xbox pad — see
 *     `applyGamepadLayout` / `ensureGamepadConfigForApp`. This is the
 *     layout Microsoft's official Steam Deck + Edge xCloud guide mandates;
 *     without it the default keyboard/mouse layout leaves menus navigable
 *     (trackpad-mouse) but sends no gamepad input to the streamed game.
 *
 * Standards compliance : the previous version reached into
 * `(window as any).appStore` directly, bypassing the
 * SteamBridge isolation layer. This refactor delegates to
 * `getShortcutRunGameId` which lives inside SteamBridge —
 * any future change to the Steam internal path is then
 * a one-file edit.
 *
 * ── Investigation (2026-06, beta feedback) ──────────────────
 * Beta testers asked for the Unifideck-Launcher / auth-window
 * shortcut to come up with a "Web Browser" controller layout
 * (keyboard/mouse-like bindings) so the in-prefix login UI is
 * navigable. Re-checked the available surface:
 *
 *   • `@decky/ui` ships NO typed `SteamClient.Input.*` surface,
 *     and our `types/steam.ts` declares only `Apps` +
 *     `GameSessions`. The vestigial `ControllerConfigInfo*`
 *     types there describe the read-only template-list *stream*
 *     — there is no verified setter binding.
 *   • The only controller method on the typed `Apps` surface is
 *     `ShowControllerConfigurator(appId)`, which pops Steam's
 *     full configurator UI — too intrusive to fire mid-auth.
 *   • The historical programmatic path (set-active-config /
 *     template apply) is the exact code that raced Steam and
 *     threw "Unknown method"; it was deliberately reverted.
 *
 * Update (2026-06): a typed `SteamClient.Input` setter surface WAS
 * confirmed. `RegisterForControllerConfigInfoMessages` +
 * `QueryControllerConfigsForApp` + `SetSelectedConfigForApp` were verified
 * against the Steam UI bundle and exercised live in Gaming Mode (the
 * "Unknown method" failures came from the old configurator-popup path, not
 * these). Rather than hardcode the template URL (produced at runtime) we
 * enumerate the live config list and pick the matching official entry
 * (`controller_neptune_webbrowser.vdf` for auth,
 * `controller_neptune_gamepad_fps.vdf` for xCloud). Every call is wrapped
 * so any failure leaves the launch unaffected; applied BEFORE `RunGame`.
 */
import { getShortcutRunGameId } from "../lib/steam-bridge";
import type {
  ControllerConfigInfoMessage,
  ControllerConfigInfoMessageList,
} from "../types/steam";

const LOG_PREFIX = "[ControllerConfig]";
// The Deck's built-in controller is index 0 in Gaming Mode.
const PRIMARY_CONTROLLER_INDEX = 0;
// How long to wait for Steam to stream the template list before giving up.
const CONFIG_INFO_TIMEOUT_MS = 4000;

/** Default an xCloud shortcut to the "Gamepad With Joystick Trackpad"
 *  layout. Best-effort and fire-and-forget: a failed/absent apply just
 *  leaves the current layout. Run *after* `RunGame` (see
 *  `launchAppWithControllerLayout`). Steam's controller-config API is
 *  inert for an idle shortcut, so we apply once the app is the active
 *  launch target; the selection persists for subsequent launches. */
export async function ensureGamepadConfigForApp(appId: number): Promise<void> {
  const applied = await applyGamepadLayout(appId);
  console.log(
    `${LOG_PREFIX} gamepad layout for appId=${appId}: ` +
      `${applied ? "applied" : "kept default"}`,
  );
}

/** Which Steam Input template a browser game's shortcut defaults to.
 *  `gamepad` (Gamepad With Joystick Trackpad) drives an xCloud stream as an
 *  Xbox pad; `web-browser` gives an HTML5 game a trackpad mouse. */
export type BrowserGameLayout = "gamepad" | "web-browser";

/** Launch a browser game's shortcut through Steam's RunGame API, then
 *  default it to `layout`. Returns false when Steam's Apps surface is
 *  unavailable (test environments, very early plugin boot).
 *
 *  The layout is applied AFTER `RunGame`: Steam's controller-config API is
 *  inert for an idle shortcut. Fire-and-forget, so it never delays the
 *  launch; the selection persists for later launches. */
export async function launchAppWithControllerLayout(
  appId: number,
  layout: BrowserGameLayout,
): Promise<boolean> {
  const steamApps = window.SteamClient?.Apps;
  if (!steamApps?.RunGame) {
    return false;
  }

  steamApps.RunGame(getShortcutRunGameId(appId), "", -1, 100);
  console.log(`${LOG_PREFIX} Launched appId=${appId} (${layout} layout)`);
  if (layout === "gamepad") {
    void ensureGamepadConfigForApp(appId);
  } else {
    applyWebBrowserLayout(appId);
  }

  return true;
}

/** Reserved for the day a one-shot post-launch hand-off is
 *  needed (e.g. to consume a deferred config payload). The
 *  current implementation is a no-op so callers get a stable
 *  API while the underlying signal is being designed. */
export function consumeConfiguredLaunch(_appId: number): boolean {
  return false;
}
/** Reset any in-process caches the controller-config layer
 *  may have accumulated. No-op today; kept for symmetry with
 *  the public surface the legacy module exposed. */
export function resetControllerConfigCache(): void {
  /* intentionally empty */
}

/** A config-info message that carries a template (vs. a "Done" marker). */
function isTemplateEntry(
  m: ControllerConfigInfoMessage,
): m is ControllerConfigInfoMessageList {
  return (
    "URL" in m && typeof (m as ControllerConfigInfoMessageList).URL === "string"
  );
}

/** Predicate selecting one official template from the live config stream. */
type TemplateMatcher = (m: ControllerConfigInfoMessageList) => boolean;

/** The official Steam "Web Browser" template (by title or filename). */
function isWebBrowserTemplate(m: ControllerConfigInfoMessageList): boolean {
  return (
    m.bOfficial && (m.Title === "Web Browser" || /webbrowser/i.test(m.URL))
  );
}

/**
 * The official "Gamepad With Joystick Trackpad" template
 * (`controller_neptune_gamepad_fps.vdf`). This is the layout Microsoft's
 * own "Xbox Cloud Gaming in Microsoft Edge with Steam Deck" guide tells
 * users to apply: it maps the Deck's physical controls to the virtual
 * Xbox pad (right trackpad → right stick) so xCloud forwards real gamepad
 * input to the streamed game. Without it the Deck defaults to a
 * keyboard/mouse layout — menus stay navigable via the trackpad-mouse but
 * the game itself receives no controller input. Matched by filename
 * (stable) or localized title.
 */
function isGamepadJoystickTemplate(
  m: ControllerConfigInfoMessageList,
): boolean {
  return (
    m.bOfficial &&
    (/gamepad_fps/i.test(m.URL) || /gamepad\s+with\s+joystick/i.test(m.Title))
  );
}

/**
 * Shared core: stream the app's controller configs, pick the first
 * official template matching `matches`, and persist it for the app via
 * `SetSelectedConfigForApp`. Resolves `true` when a template was applied,
 * `false` on unavailable API / no match / timeout / error. Never throws.
 *
 * Flow (all verified against the Steam UI bundle, and the live API
 * confirmed callable in Gaming Mode):
 *   1. Register for the app's controller-config info stream.
 *   2. `QueryControllerConfigsForApp` to make Steam emit it.
 *   3. Pick the matching official entry and apply its `URL` via
 *      `SetSelectedConfigForApp` (which persists the selection).
 *
 * Fully guarded and non-blocking: any failure leaves the launch
 * completely unaffected (the app still launches, just on the default
 * layout). A timeout unregisters the listener if Steam never streams a
 * match. Call this BEFORE `RunGame` (and await it if the current launch
 * must pick up the layout).
 */
function applySelectedTemplate(
  appId: number,
  matches: TemplateMatcher,
  label: string,
): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    try {
      const input = window.SteamClient?.Input;
      if (
        !input?.RegisterForControllerConfigInfoMessages ||
        !input?.QueryControllerConfigsForApp ||
        !input?.SetSelectedConfigForApp
      ) {
        console.log(
          `${LOG_PREFIX} SteamClient.Input config API unavailable — ` +
            `default layout kept for appId=${appId}`,
        );
        resolve(false);
        return;
      }
      const idx = PRIMARY_CONTROLLER_INDEX;
      // Mutable holder so `finish` can close over the registration +
      // timer that are created after it (avoids a let/const cycle).
      const state: {
        settled: boolean;
        reg?: { unregister(): void };
        timer?: ReturnType<typeof setTimeout>;
      } = { settled: false };
      const finish = (ok: boolean) => {
        if (state.timer !== undefined) clearTimeout(state.timer);
        try {
          state.reg?.unregister();
        } catch {
          /* ignore */
        }
        resolve(ok);
      };

      state.reg = input.RegisterForControllerConfigInfoMessages(
        appId,
        (messages) => {
          if (state.settled || !Array.isArray(messages)) return;
          const tpl = messages.filter(isTemplateEntry).find(matches);
          if (!tpl) return;
          state.settled = true;
          try {
            input.SetSelectedConfigForApp(appId, idx, tpl.URL, false, true);
            console.log(
              `${LOG_PREFIX} applied ${label} to ` +
                `appId=${appId} (${tpl.URL})`,
            );
            finish(true);
          } catch (e) {
            console.warn(`${LOG_PREFIX} SetSelectedConfigForApp failed:`, e);
            finish(false);
          }
        },
      );
      input.QueryControllerConfigsForApp(appId, idx, false);
      state.timer = setTimeout(() => {
        if (!state.settled) {
          console.log(
            `${LOG_PREFIX} ${label} template not found for ` +
              `appId=${appId} within ${CONFIG_INFO_TIMEOUT_MS}ms — ` +
              `default layout kept`,
          );
        }
        finish(false);
      }, CONFIG_INFO_TIMEOUT_MS);
    } catch (e) {
      console.warn(`${LOG_PREFIX} applySelectedTemplate error (ignored):`, e);
      resolve(false);
    }
  });
}

/**
 * Apply the official Steam "Web Browser" controller template to the
 * temporary auth-window shortcut so the store-login page is navigable
 * with the trackpad/stick (mouse) instead of a useless gamepad binding.
 * Fire-and-forget: applied AFTER the temp shortcut's app entry exists and
 * BEFORE `RunGame`; failure leaves login unaffected.
 */
export function applyWebBrowserLayout(appId: number): void {
  void applySelectedTemplate(appId, isWebBrowserTemplate, "Web Browser layout");
}

/**
 * Apply the official "Gamepad With Joystick Trackpad" template to an
 * xCloud shortcut so the Steam Deck controller drives the streamed game
 * as an Xbox pad (see `isGamepadJoystickTemplate`). Resolves once the
 * layout is applied (or the attempt times out) so the caller can await it
 * before `RunGame`, ensuring the current launch picks up the layout. The
 * selection persists, so later launches keep it.
 */
export function applyGamepadLayout(appId: number): Promise<boolean> {
  return applySelectedTemplate(
    appId,
    isGamepadJoystickTemplate,
    "Gamepad (Joystick Trackpad) layout",
  );
}
