/**
 * Scoped CSS for the STORE CONNECTIONS rows and their options modal.
 *
 * Rendered ONCE by `StoreConnections`, not per row: the QAM is its own
 * CEF document, so the styles have to travel with the component, but
 * they only need to arrive once (same reason `play.css.ts` exists).
 *
 * Each store is one row — icon, name, one line of status, options button.
 * The button opens `StoreOptionsModal`, which holds the full-width actions.
 * The status and button columns are fixed-width so every row lines up, and
 * long status text is cut off with an ellipsis before it can push the button.
 *
 * Only the options button takes focus; the rest of the row is a label.
 */

export const STORE_ROW_CSS = `
.unifideck-store-row-header {
  display: flex;
  align-items: center;
  gap: 8px;
  /* Compact, but still a comfortable touch target. */
  min-height: 34px;
  padding-block: 2px;
  padding-inline: 6px;
  border-radius: 4px;
}
.unifideck-store-row-name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.unifideck-store-row-status {
  flex: 0 0 118px;
  font-size: 12px;
  text-align: end;
  opacity: 0.7;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.unifideck-store-row-status.error {
  color: #ff6b6b;
  opacity: 1;
}

/* The row's only focus target besides its own text: the options button. */
.unifideck-store-row-options {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 0 !important;
  width: 32px;
  height: 28px;
  padding: 0 !important;
}

.unifideck-store-modal-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 4px;
}

/* Actions sit under the row with the same side margins as the row itself,
   and a little room above the first button. */
.unifideck-store-row-actions {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-block: 10px;
  padding-inline: 6px;
}
.unifideck-store-row-actions .unifideck-store-action {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  min-width: 0;
}
.unifideck-store-row-detail {
  font-size: 12px;
  opacity: 0.7;
  overflow-wrap: anywhere;
}
.unifideck-store-row-detail.error {
  color: #ff6b6b;
  opacity: 1;
}

/* Sign out is the one destructive action: red, inverting on focus. */
.unifideck-store-auth-button.connected {
  background-color: #ef4444 !important;
  color: #fff;
}
.unifideck-store-auth-button.connected:focus,
.unifideck-store-auth-button.connected:hover,
.unifideck-store-auth-button.connected.gpfocus {
  color: #ef4444 !important;
  background-color: #fff !important;
}

/* The storefront stays disabled for a few seconds while Steam brings the
   window up. Dim it so the button doesn't look broken in the meantime. */
.unifideck-store-shop-button:disabled {
  opacity: 0.5;
}
`;
