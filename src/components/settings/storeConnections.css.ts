/**
 * Scoped CSS for the STORE CONNECTIONS rows.
 *
 * Rendered ONCE by `StoreConnections`, not per row: the QAM is its own
 * CEF document, so the styles have to travel with the component, but
 * they only need to arrive once (same reason `play.css.ts` exists).
 *
 * Each store is one row — icon, name, one line of status, chevron — that
 * expands to full-width action buttons. The status and chevron columns
 * are fixed-width so every row lines up, and long status text is cut off
 * with an ellipsis before it can push the chevron.
 *
 * The header's focus treatment is load-bearing: the whole row is one
 * gamepad target, and the user has to see at arm's length which row the
 * D-pad is on.
 */

export const STORE_ROW_CSS = `
.unifideck-store-row-header {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 40px;
  padding-block: 4px;
  padding-inline: 6px;
  border-radius: 4px;
  cursor: pointer;
}
.unifideck-store-row-header:hover,
.unifideck-store-row-header:focus,
.unifideck-store-row-header.gpfocus {
  background: rgba(255, 255, 255, 0.12);
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

/* One down-pointing icon, rotated to point at the row's end while
   collapsed. It never moves, only turns. The rotation flips under RTL so
   the collapsed chevron always points away from the text. */
.unifideck-store-row-chevron {
  flex: 0 0 16px;
  display: inline-flex;
  justify-content: center;
  transform: rotate(-90deg);
  transition: transform 0.15s ease;
}
[dir="rtl"] .unifideck-store-row-chevron,
.unifideck-store-row-chevron:dir(rtl) {
  transform: rotate(90deg);
}
.unifideck-store-row.expanded .unifideck-store-row-chevron {
  transform: rotate(0deg);
}

/* Actions sit under the row, indented to line up with the store name. */
.unifideck-store-row-actions {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-block: 4px 10px;
  padding-inline-start: 32px;
  padding-inline-end: 6px;
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
