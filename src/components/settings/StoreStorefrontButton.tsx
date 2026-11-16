/**
 * StoreStorefrontButton — "Open store", one of the full-width actions in
 * a store's options modal. Opens that store's shop with the
 * session the user already has.
 *
 * Shown ONLY when the store is connected. `isConnected` arrives as a
 * prop rather than being re-derived from `status` here, so the cart and
 * the sign-out button cannot disagree about whether the user is signed
 * in — they read the same value, computed once in `StoreRow`. A cart
 * next to a "Sign in" button would be nonsense: there is no session to
 * shop with.
 *
 * Sized like every other row action by `.unifideck-store-action` in
 * `storeConnections.css.ts`, rendered once by the parent.
 */
import { FC, useCallback, useEffect, useState } from "react";
import { DialogButton, showModal } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FiShoppingCart } from "react-icons/fi";
import { ChromiumInstallModal } from "../modals/ChromiumInstallModal";
import {
  BROWSER_MISSING,
  openStorefront,
} from "../../services/store/StorefrontLauncher";
import { useToast } from "../../hooks/useToast";
import { toastText, headerText } from "../../services/toast-text";
import { STORE_VISUALS } from "../../types/store";
import type { StoreId } from "../../types/api";

/**
 * How long the button stays disabled after a press.
 *
 * Steam takes a moment to bring the window up, and a second press in
 * that gap launches a second shortcut that Chromium immediately hands
 * off to the first instance and kills — leaving a phantom app flashing
 * in the running-games row. Long enough to cover that, short enough
 * that a launch which failed outright is retryable.
 */
const PRESS_LOCKOUT_MS = 5000;

/**
 * When each store's shop was last opened. The button lives in a modal that
 * closes on press, so its own state would be gone by the next time it is
 * shown; the lockout has to outlive it.
 */
const lastOpened = new Map<StoreId, number>();

const lockedOut = (store: StoreId): boolean =>
  Date.now() - (lastOpened.get(store) ?? 0) < PRESS_LOCKOUT_MS;

interface Props {
  store: StoreId;
  /** The row's single signed-in verdict. Computed in `StoreRow`. */
  isConnected: boolean;
  busy?: boolean;
  /** Called on press, before the shop opens (e.g. to close a modal). */
  onPress?: () => void;
}

export const StoreStorefrontButton: FC<Props> = ({
  store,
  isConnected,
  busy,
  onPress,
}) => {
  const { t } = useTranslation();
  const toast = useToast();
  const [opening, setOpening] = useState(() => lockedOut(store));
  useEffect(() => {
    if (!opening) return undefined;
    const left = PRESS_LOCKOUT_MS - (Date.now() - (lastOpened.get(store) ?? 0));
    const id = window.setTimeout(() => setOpening(false), Math.max(left, 0));
    return () => window.clearTimeout(id);
  }, [opening, store]);
  // Brand name, not a translatable word — interpolated into the
  // localized sentence, same source of truth as every other store label.
  const storeName = STORE_VISUALS[store]?.display_name ?? store;

  const open = useCallback(async () => {
    lastOpened.set(store, Date.now());
    setOpening(true);
    toast.info(
      headerText("toasts.header.store", { storeName }),
      toastText("toasts.storefrontOpening"),
    );
    let error: unknown = null;
    try {
      const result = await openStorefront(store);
      if (result.error === BROWSER_MISSING) {
        // Same prompt a browser sign-in gives; the shop opens once it's in.
        showModal(
          <ChromiumInstallModal
            onInstalled={() => void open()}
            closeModal={() => {}}
          />,
        );
        return;
      }
      if (!result.success) error = result.error;
    } catch (e) {
      error = e;
    }
    if (error !== null) {
      console.error(
        `[StoreStorefrontButton] ${store} storefront failed:`,
        error,
      );
      toast.error(
        headerText("toasts.header.store", { storeName }),
        toastText("toasts.storefrontFailed"),
      );
    }
  }, [store, storeName, toast]);

  if (!isConnected) return null;
  return (
    <DialogButton
      disabled={busy || opening}
      className="unifideck-store-action unifideck-store-shop-button"
      onClick={() => {
        onPress?.();
        void open();
      }}
    >
      <FiShoppingCart />
      {t("storeConnections.openStore")}
    </DialogButton>
  );
};
