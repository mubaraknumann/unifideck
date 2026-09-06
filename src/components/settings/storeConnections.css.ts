/**
 * Scoped CSS for the STORE CONNECTIONS rows.
 *
 * Rendered ONCE by `StoreConnections`, not per row. The red sign-out
 * rules used to live in an inline `<style>` inside `StoreAuthButton`,
 * which meant the browser parsed the same block six times — once per
 * store. Same reason `play.css.ts` exists: the QAM is its own CEF
 * document, so the styles have to travel with the component, but they
 * only need to arrive once.
 *
 * Each row now holds two adjacent buttons (cart, then sign-out), and
 * that is what makes the focus ring load-bearing rather than
 * decoration: with two targets side by side the user has to be able to
 * tell, at arm's length on a handheld, which one the D-pad is on. The
 * ring treatment is lifted from `play.css.ts` — solid white inner ring,
 * dark outer ring so it has contrast against any background, plus a
 * small scale-up that reads in peripheral vision.
 */

export const STORE_ROW_CSS = `
/* ── Sign out / Sign in ──────────────────────────────────────────── */
/* Red only in the connected (sign-out) state; the disconnected state
   keeps Steam's neutral button, because it reads "Sign in". */
.unifideck-store-auth-button.connected {
  background-color: #ef4444 !important;
  color: #fff;
}
.unifideck-store-auth-button.connected:focus,
.unifideck-store-auth-button.connected:hover {
  color: #ef4444 !important;
  background-color: #fff !important;
}

/* ── Shop (cart) ─────────────────────────────────────────────────── */
/* Neutral fill: the shop is not a destructive action and must not
   compete with the red button beside it for attention. */
.unifideck-store-shop-button {
  transition: background 0.15s ease, box-shadow 0.15s ease !important;
}
.unifideck-store-shop-button:hover,
.unifideck-store-shop-button:focus,
.unifideck-store-shop-button:focus-within,
.unifideck-store-shop-button.gpfocus {
  background: #ffffff !important;
  color: #23262e !important;
  box-shadow:
    0 0 0 3px #ffffff,
    0 0 0 6px rgba(0, 0, 0, 0.7) !important;
  transform: scale(1.05);
  /* position is required for z-index to apply, so the ring paints over
     the neighbouring button instead of being clipped by it. */
  position: relative;
  z-index: 1;
}

/* A press stays disabled for a few seconds while Steam brings the
   window up. Dim it so the row doesn't look broken in the meantime. */
.unifideck-store-shop-button:disabled {
  opacity: 0.5;
}
`;
