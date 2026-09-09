/**
 * Regression: a store row must never render without an auth button.
 *
 * `StoreAuthButton` used to return null for a status of `"error"`. The only
 * producer of that status is `auth-store`'s `store_auth_failed` subscription,
 * so any backend emit of `STORE_AUTH_FAILED` blanked the store's row — no
 * sign-in button, and (via `isConnected`) no storefront button either. It was
 * sticky for the session and replayed across reloads. Measured on-device
 * 2026-09-05: the Ubisoft post-play credential check emitted that event to say
 * "sign out and back in", which removed the button that does exactly that.
 *
 * The rule, stated in `py_modules/unifideck/stores/shared/wrapper_auth_monitor.py`:
 * the one answer worse than a button that does nothing is a button that is not
 * there at all. Anything that is not "connected" offers Connect.
 *
 * These call the component as a plain function — React here is the
 * `test-support` stub, so JSX yields a descriptor object and nothing renders.
 * That is enough to pin which branch the component takes.
 */
import { describe, it, expect, vi } from "vitest";

vi.mock("@decky/ui", () => ({ DialogButton: "DialogButton" }));
vi.mock("react-icons/fi", () => ({ FiLogOut: "FiLogOut" }));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { StoreAuthButton } from "./StoreAuthButton";
import type { StoreId } from "../../types/api";

type Descriptor = { type?: unknown; props?: Record<string, unknown> };

/** Depth-first search for the single node carrying an `onClick`. */
function findClickable(node: unknown): Record<string, unknown> | undefined {
  if (!node || typeof node !== "object") return undefined;
  const props = (node as Descriptor).props;
  if (props && typeof props.onClick === "function") return props;
  const children = props?.children;
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    const hit = findClickable(child);
    if (hit) return hit;
  }
  return undefined;
}

function render(status: string) {
  const onConnect = vi.fn();
  const onDisconnect = vi.fn();
  const out = StoreAuthButton({
    store: "ubisoft" as StoreId,
    status,
    onConnect,
    onDisconnect,
  }) as unknown;
  return { out, onConnect, onDisconnect, clickable: findClickable(out) };
}

describe("StoreAuthButton", () => {
  // "checking" is not in the `StoreStatus` union and no backend emits it; it
  // is here because it was the other half of the early return that shipped.
  for (const status of ["error", "expired", "disconnected", "checking", ""]) {
    it(`renders a Connect button for status "${status}"`, () => {
      const { out, clickable, onConnect, onDisconnect } = render(status);
      expect(out).not.toBeNull();
      expect(clickable).toBeDefined();

      (clickable!.onClick as () => void)();
      expect(onConnect).toHaveBeenCalledWith("ubisoft");
      expect(onDisconnect).not.toHaveBeenCalled();
    });
  }

  it("renders a Disconnect button when connected", () => {
    const { clickable, onConnect, onDisconnect } = render("connected");
    expect(clickable).toBeDefined();

    (clickable!.onClick as () => void)();
    expect(onDisconnect).toHaveBeenCalledWith("ubisoft");
    expect(onConnect).not.toHaveBeenCalled();
  });
});
