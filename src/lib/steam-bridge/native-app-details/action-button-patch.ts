/**
 * Steam's own action button (Play / Install / Update / …) for our games.
 *
 * Two seams, both verified live on 2026-11-02:
 *
 *  1. The button is an exported `React.forwardRef` (factory source holds
 *     `bShowStreamingSelector`, `ShowStreamingMenu` and
 *     `strGameActionDetailsOverride`). React reads `.render` on every
 *     render, so wrapping it reaches every button instance. For our app ids
 *     the wrapper hands the native element to a host component, which gives
 *     it a proxied overview carrying our display status (see
 *     `overview-proxy.ts`). The host is a different element type, so the
 *     native class instance under it is always created after this patch.
 *
 *  2. Its inner class decorates `OnClick` with a bind getter that caches a
 *     bound copy on the instance at first access. `render` cannot be
 *     patched (MobX freezes a per-instance render after the first paint),
 *     but the prototype getter can: ours installs a per-instance function
 *     that runs our handler when the overview is one of our proxies and
 *     Steam's own otherwise.
 *
 * The same button renders in Gaming Mode App-Details, the desktop play bar
 * and a few library surfaces, so all of them get our handlers. If the click
 * seam cannot be patched the button is left completely stock: a proxied
 * overview with Steam's click would open Steam's install wizard.
 */
import { isValidElement, type ReactElement } from "react";
import { isUnifideckAppId } from "./gate";
import { actionSpecFor, isOverviewLive } from "./overview-proxy";
import { patchGetter, patchSlot } from "./slot-patch";
import {
  findExportInModule,
  isClassComponent,
  isForwardRef,
  type ForwardRefObject,
} from "./webpack";

export type ActionHostRenderer = (
  nativeElement: ReactElement,
  appId: number,
) => ReactElement;

const ACTION_BUTTON_NEEDLES = [
  "bShowStreamingSelector",
  "ShowStreamingMenu",
  "strGameActionDetailsOverride",
] as const;

export function findActionButton(): ForwardRefObject | null {
  return findExportInModule(ACTION_BUTTON_NEEDLES, isForwardRef);
}

interface ButtonInstance {
  props?: { overview?: unknown };
}

type ClickFn = (...args: unknown[]) => unknown;

/** Patch `OnClick` on the native button class. Throws when it cannot. */
function patchClick(cls: unknown): () => void {
  if (!isClassComponent(cls)) throw new Error("button is not a class");
  return patchGetter(
    cls.prototype,
    "OnClick",
    (originalGet) =>
      function getOnClick(this: unknown) {
        const self = this as ButtonInstance;
        // The original getter binds and caches on whatever `this` it gets.
        // A shim that inherits from the instance gives us Steam's handler
        // without letting it claim the instance's own `OnClick` slot.
        const native = originalGet.call(
          Object.create(self as object),
        ) as ClickFn;
        const onClick: ClickFn = (...args) => {
          const overview = self.props?.overview;
          const spec = actionSpecFor(overview);
          if (!spec || isOverviewLive(overview)) return native(...args);
          try {
            spec.onPrimary();
          } catch (e) {
            console.error("[Unifideck NativeDetails] action failed:", e);
          }
          const event = args[1] as
            | { stopPropagation?: () => void; preventDefault?: () => void }
            | undefined;
          event?.stopPropagation?.();
          event?.preventDefault?.();
          return undefined;
        };
        Object.defineProperty(self, "OnClick", {
          value: onClick,
          configurable: true,
          writable: true,
        });
        return onClick;
      },
  );
}

/**
 * Install the action-button patch. Returns its disposer, or `null` when
 * the button cannot be found (the caller then keeps the legacy UI).
 */
export function patchActionButton(
  renderHost: ActionHostRenderer,
): (() => void) | null {
  const button = findActionButton();
  if (!button) return null;
  let unpatchClick: (() => void) | null = null;
  let clickBroken = false;
  const unpatchRender = patchSlot<ForwardRefObject["render"]>(
    button,
    "render",
    (original) =>
      function render(this: unknown, props, ref) {
        const el = original.call(this, props, ref);
        if (clickBroken || !isValidElement(el)) return el;
        try {
          const appId = (props?.overview as { appid?: unknown } | undefined)
            ?.appid;
          if (!isUnifideckAppId(appId)) return el;
          if (!unpatchClick) unpatchClick = patchClick(el.type);
          return renderHost(el, appId);
        } catch (e) {
          clickBroken = true;
          console.error(
            "[Unifideck NativeDetails] action button left stock:",
            e,
          );
          return el;
        }
      },
  );
  return () => {
    unpatchRender();
    unpatchClick?.();
  };
}
