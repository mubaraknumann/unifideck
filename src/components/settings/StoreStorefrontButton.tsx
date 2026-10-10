/**
 * StoreStorefrontButton — "Open store", one of the full-width actions in
 * an expanded `StoreConnections` row. Opens that store's shop with the
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
import { FC, useCallback, useState } from "react";
import { DialogButton } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FiShoppingCart } from "react-icons/fi";
import { openStorefront } from "../../services/store/StorefrontLauncher";
import { useToast } from "../../hooks/useToast";
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

interface Props {
  store: StoreId;
  /** The row's single signed-in verdict. Computed in `StoreRow`. */
  isConnected: boolean;
  busy?: boolean;
}

export const StoreStorefrontButton: FC<Props> = ({
  store,
  isConnected,
  busy,
}) => {
  const { t } = useTranslation();
  const toast = useToast();
  const [opening, setOpening] = useState(false);
  // Brand name, not a translatable word — interpolated into the
  // localized sentence, same source of truth as every other store label.
  const storeName = STORE_VISUALS[store]?.display_name ?? store;

  const open = useCallback(async () => {
    setOpening(true);
    window.setTimeout(() => setOpening(false), PRESS_LOCKOUT_MS);
    toast.info(t("storeConnections.storefrontOpening", { store: storeName }));
    try {
      const result = await openStorefront(store);
      if (!result.success) {
        toast.error(
          t("storeConnections.storefrontFailed", { store: storeName }),
          result.error,
        );
      }
    } catch (e) {
      toast.error(
        t("storeConnections.storefrontFailed", { store: storeName }),
        e instanceof Error ? e.message : String(e),
      );
    }
  }, [store, storeName, toast, t]);

  if (!isConnected) return null;
  return (
    <DialogButton
      disabled={busy || opening}
      className="unifideck-store-action unifideck-store-shop-button"
      onClick={() => void open()}
    >
      <FiShoppingCart />
      {t("storeConnections.openStore")}
    </DialogButton>
  );
};
